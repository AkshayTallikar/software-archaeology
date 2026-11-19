"""Mutation testing: does the recording notice a small change to a rebuild?

A *mutant* is a copy of a rebuild with exactly one change. ``generate_mutants``
finds every change the operators below can make; ``run_mutation`` replays the
full recording against each mutant, in its own fresh process, using the same
replay engine that proves the rebuild.

* **killed**: replay is not 100% equivalent, so the recording noticed the change.
* **survived**: every recorded exchange still matches. Either the change is
  equivalent by construction, or the recording does not pin that rule.
* **error**: the mutant failed to import or start, or it timed out. Errors are
  reported apart and are not counted in the score.

Mutation score = killed / (killed + survived).

Operators:

* ``int-literal``: an int literal n becomes n+1 and n-1 (0 becomes -1 as well;
  the evidence decides whether that matters).
* ``money-literal``: a decimal literal moves by one unit in its last decimal
  place, +1 and -1. Covers floats such as ``0.25`` and strings such as
  ``"10.00"`` that are read as money.
* ``comparison``: ``<`` and ``<=``, ``>`` and ``>=``, ``==`` and ``!=`` swap.
* ``boolean``: ``and`` and ``or`` swap.
* ``guard-order``: two consecutive ``if <cond>: return ...`` guards in one
  function body swap places.

Skipped by construction, so no mutant is generated for them:

* docstrings and every other bare expression statement (no effect at run time);
* type annotations (they describe values; they do not compute them);
* booleans, ``None`` and ``Ellipsis`` (not numeric literals);
* strings that are not decimal numbers.

Status codes passed as ``status_code=`` are integer literals and are mutated
like any other, because they are behaviour the client sees.

Mutant ids are ``L<line>:C<col>:<operator>``, plus a suffix that tells apart
several changes at one position. Ids depend only on the source text, so the
same rebuild gives the same ids on every run.

Run as ``python -m archaeology.mutate --worker TARGET TRACES SPEC|-`` by the
scorer: the worker replays the recording against one target and prints a JSON
line with the matched and total counts.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from archaeology.infer.spec import load_spec, volatile_paths_for
from archaeology.replay import load_target, replay
from archaeology.trace import read_traces

OPERATORS: tuple[str, ...] = ("int-literal", "money-literal", "comparison", "boolean", "guard-order")

REPO_ROOT = Path(__file__).resolve().parent.parent

_COMPARISON_SWAPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}
_BOOLEAN_SWAPS: dict[type[ast.boolop], type[ast.boolop]] = {ast.And: ast.Or, ast.Or: ast.And}
_MONEY_TEXT = re.compile(r"\d+\.\d+")
_SNIPPET_LIMIT = 120


@dataclass(frozen=True)
class Mutant:
    """One single-change variant of a rebuild."""

    id: str
    operator: str
    line: int
    function: str
    original: str
    mutated: str


@dataclass
class _Site:
    """A place where a mutant can be made: its description, sort column and the change itself."""

    mutant: Mutant
    col: int
    apply: Callable[[], None]


# ---------------------------------------------------------------------------
# Finding mutation sites
# ---------------------------------------------------------------------------


def _snippet(node: ast.AST) -> str:
    """The node as one line of source, cut short for the report."""
    text = ast.unparse(node).splitlines()[0]
    return text if len(text) <= _SNIPPET_LIMIT else text[: _SNIPPET_LIMIT - 3] + "..."


def _function_names(tree: ast.AST) -> dict[int, str]:
    """The name of the innermost enclosing function for every node (``<module>`` at top level)."""
    names: dict[int, str] = {}

    def visit(node: ast.AST, name: str) -> None:
        names[id(node)] = name
        child_name = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
        for child in ast.iter_child_nodes(node):
            visit(child, child_name)

    visit(tree, "<module>")
    return names


def _annotations_of(node: ast.AST) -> Iterator[ast.expr]:
    if isinstance(node, ast.arg) and node.annotation is not None:
        yield node.annotation
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
        yield node.returns
    elif isinstance(node, ast.AnnAssign):
        yield node.annotation


def _skipped_ids(tree: ast.AST) -> set[int]:
    """Ids of nodes that no mutant may change: bare expressions and annotations."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skipped.add(id(node.value))
        for annotation in _annotations_of(node):
            skipped.update(id(child) for child in ast.walk(annotation))
    return skipped


def _make_site(
    node: ast.AST,
    function: str,
    operator: str,
    suffix: str,
    original: str,
    mutated: str,
    apply: Callable[[], None],
) -> _Site:
    line, col = node.lineno, node.col_offset  # type: ignore[attr-defined]
    mutant = Mutant(
        id=f"L{line}:C{col}:{operator}{suffix}",
        operator=operator,
        line=line,
        function=function,
        original=original,
        mutated=mutated,
    )
    return _Site(mutant=mutant, col=col, apply=apply)


def _money_unit(value: object) -> tuple[Decimal, bool] | None:
    """The last decimal place of a money-looking literal as ``(unit, is_float)``, or ``None``."""
    if isinstance(value, float):
        text = repr(value)
        if "e" in text or "." not in text:
            return None
        return Decimal(1).scaleb(-len(text.split(".")[1])), True
    if isinstance(value, str) and _MONEY_TEXT.fullmatch(value):
        return Decimal(1).scaleb(-len(value.split(".")[1])), False
    return None


def _literal_sites(node: ast.Constant, function: str) -> list[_Site]:
    value = node.value
    if isinstance(value, bool) or value is None or value is Ellipsis:
        return []
    if isinstance(value, int):
        return [_int_site(node, function, value, delta) for delta in (1, -1)]
    money = _money_unit(value)
    if money is None:
        return []
    unit, is_float = money
    return [_money_site(node, function, value, unit, is_float, delta) for delta in (1, -1)]


def _int_site(node: ast.Constant, function: str, value: int, delta: int) -> _Site:
    def apply() -> None:
        node.value = value + delta

    return _make_site(
        node, function, "int-literal", f"{delta:+d}", str(value), str(value + delta), apply
    )


def _money_site(
    node: ast.Constant, function: str, value: object, unit: Decimal, is_float: bool, delta: int
) -> _Site:
    if is_float:
        base = Decimal(repr(value))
        moved = base + delta * unit
        new_value: object = float(moved)
        original, mutated = repr(value), repr(new_value)
    else:
        base = Decimal(str(value))
        moved = base + delta * unit
        decimals = len(str(value).split(".")[1])
        new_value = f"{moved:.{decimals}f}"
        original, mutated = str(value), str(new_value)

    def apply() -> None:
        node.value = new_value

    return _make_site(node, function, "money-literal", f"{delta:+d}", original, mutated, apply)


def _compare_apply(node: ast.Compare, index: int, swap: type[ast.cmpop]) -> Callable[[], None]:
    def apply() -> None:
        node.ops[index] = swap()

    return apply


def _compare_sites(node: ast.Compare, function: str) -> list[_Site]:
    sites: list[_Site] = []
    for index, op in enumerate(node.ops):
        swap = _COMPARISON_SWAPS.get(type(op))
        if swap is None:
            continue
        changed = copy.deepcopy(node)
        changed.ops[index] = swap()
        sites.append(
            _make_site(
                node,
                function,
                "comparison",
                f"#{index}:{type(op).__name__}->{swap.__name__}",
                _snippet(node),
                _snippet(changed),
                _compare_apply(node, index, swap),
            )
        )
    return sites


def _boolop_apply(node: ast.BoolOp, swap: type[ast.boolop]) -> Callable[[], None]:
    def apply() -> None:
        node.op = swap()

    return apply


def _boolop_sites(node: ast.BoolOp, function: str) -> list[_Site]:
    swap = _BOOLEAN_SWAPS.get(type(node.op))
    if swap is None:
        return []
    changed = copy.deepcopy(node)
    changed.op = swap()
    return [
        _make_site(
            node,
            function,
            "boolean",
            f":{type(node.op).__name__}->{swap.__name__}",
            _snippet(node),
            _snippet(changed),
            _boolop_apply(node, swap),
        )
    ]


def _is_guard(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.If)
        and not stmt.orelse
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.Return)
    )


def _swap_apply(body: list[ast.stmt], index: int) -> Callable[[], None]:
    def apply() -> None:
        body[index], body[index + 1] = body[index + 1], body[index]

    return apply


def _guard_sites(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[_Site]:
    sites: list[_Site] = []
    body = func.body
    for index in range(len(body) - 1):
        first, second = body[index], body[index + 1]
        if not (_is_guard(first) and _is_guard(second)):
            continue
        original = f"{_head(first)} / {_head(second)}"
        mutated = f"{_head(second)} / {_head(first)}"
        sites.append(
            _make_site(first, func.name, "guard-order", "", original, mutated, _swap_apply(body, index))
        )
    return sites


def _head(stmt: ast.stmt) -> str:
    """The first line of a statement, such as ``if qty > 10:``."""
    return _snippet(stmt)


def _unique(sites: Iterable[_Site]) -> list[_Site]:
    """Suffix any repeated id with ``~n`` so every mutant has its own id."""
    seen: Counter[str] = Counter()
    unique: list[_Site] = []
    for site in sites:
        seen[site.mutant.id] += 1
        if seen[site.mutant.id] > 1:
            new_id = f"{site.mutant.id}~{seen[site.mutant.id]}"
            site = replace(site, mutant=replace(site.mutant, id=new_id))
        unique.append(site)
    return unique


def _sites(tree: ast.Module) -> list[_Site]:
    skipped = _skipped_ids(tree)
    names = _function_names(tree)
    sites: list[_Site] = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        function = names.get(id(node), "<module>")
        if isinstance(node, ast.Constant):
            sites.extend(_literal_sites(node, function))
        elif isinstance(node, ast.Compare):
            sites.extend(_compare_sites(node, function))
        elif isinstance(node, ast.BoolOp):
            sites.extend(_boolop_sites(node, function))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sites.extend(_guard_sites(node))
    sites.sort(key=lambda s: (s.mutant.line, s.col, s.mutant.id))
    return _unique(sites)


def generate_mutants(source: str) -> list[Mutant]:
    """Every single-change mutant of ``source``, in source order, with stable ids."""
    return [site.mutant for site in _sites(ast.parse(source))]


def mutate_source(source: str, mutant_id: str) -> str:
    """The source of the mutant ``mutant_id``. ``ast.unparse`` drops comments, which do not matter here."""
    tree = ast.parse(source)
    for site in _sites(tree):
        if site.mutant.id == mutant_id:
            site.apply()
            return ast.unparse(tree) + "\n"
    raise KeyError(f"no mutant {mutant_id!r} in this source")


def select_mutants(mutants: Sequence[Mutant], limit: int | None, seed: int = 0) -> list[Mutant]:
    """At most ``limit`` mutants, drawn with a fixed seed so runs are repeatable; source order is kept."""
    if limit is None or limit >= len(mutants):
        return list(mutants)
    chosen = random.Random(seed).sample(range(len(mutants)), max(limit, 0))
    return [mutants[i] for i in sorted(chosen)]


# ---------------------------------------------------------------------------
# Scoring: one fresh process per mutant
# ---------------------------------------------------------------------------


class WorkerError(RuntimeError):
    """A replay process that could not import, start or finish."""


@dataclass(frozen=True)
class Outcome:
    matched: int
    total: int


@dataclass(frozen=True)
class MutantResult:
    mutant: Mutant
    status: str  # "killed", "survived" or "error"
    matched: int = 0
    total: int = 0
    detail: str = ""


@dataclass
class MutationRun:
    """Everything a mutation report needs."""

    rebuild: str
    module: str
    traces: str
    spec: str | None
    generated: int
    baseline: Outcome
    results: list[MutantResult]
    wall_seconds: float
    jobs: int
    timeout: float
    max_mutants: int | None

    def summary(self) -> dict[str, Any]:
        return summarise(self.results)


def summarise(results: Sequence[MutantResult]) -> dict[str, Any]:
    """Counts, the mutation score and a per-operator breakdown."""
    by_operator: dict[str, dict[str, Any]] = {}
    for operator in OPERATORS:
        rows = [r for r in results if r.mutant.operator == operator]
        if rows:
            by_operator[operator] = _counts(rows)
    return {**_counts(results), "by_operator": by_operator}


def _counts(results: Sequence[MutantResult]) -> dict[str, Any]:
    killed = sum(r.status == "killed" for r in results)
    survived = sum(r.status == "survived" for r in results)
    errors = sum(r.status == "error" for r in results)
    detected = killed + survived
    return {
        "killed": killed,
        "survived": survived,
        "errors": errors,
        "score": killed / detected if detected else None,
    }


def module_name_for(path: str | Path) -> str:
    """The import name of a rebuild file, from its place under the working directory (``modern_app/main.py`` gives ``modern_app.main``)."""
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(Path.cwd().resolve())
    except ValueError:
        return resolved.stem
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else resolved.stem


def _import_root(path: Path, module: str) -> Path:
    """The directory that holds the top-level package of ``module``, so it can be imported."""
    return path.resolve().parents[module.count(".")]


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _worker_env(paths: Sequence[Path]) -> dict[str, str]:
    entries = [str(p) for p in paths] + [str(REPO_ROOT)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        entries.append(existing)
    return dict(os.environ, PYTHONPATH=os.pathsep.join(entries))


def replay_in_worker(
    target: str,
    traces: str,
    spec: str | None,
    paths: Sequence[Path],
    timeout: float,
) -> Outcome:
    """Replay ``traces`` against ``target`` in a fresh process. Raises :class:`WorkerError`."""
    command = [sys.executable, "-m", "archaeology.mutate", "--worker", target, str(traces), spec or "-"]
    try:
        proc = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=_worker_env(paths),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerError(f"timeout after {timeout:g}s") from exc
    if proc.returncode != 0:
        raise WorkerError(_last_line(proc.stderr) or f"exit code {proc.returncode}")
    try:
        data = json.loads(_last_line(proc.stdout))
        return Outcome(matched=int(data["matched"]), total=int(data["total"]))
    except (ValueError, KeyError) as exc:
        raise WorkerError("worker printed no result") from exc


def _evaluate(
    mutant: Mutant,
    source: str,
    leaf: str,
    scratch: Path,
    import_root: Path,
    traces: str,
    spec: str | None,
    timeout: float,
) -> MutantResult:
    package = "mutant_" + hashlib.sha1(mutant.id.encode("utf-8")).hexdigest()[:12]
    directory = scratch / package
    directory.mkdir()
    (directory / "__init__.py").write_text("", encoding="utf-8")
    try:
        (directory / f"{leaf}.py").write_text(mutate_source(source, mutant.id), encoding="utf-8")
    except (KeyError, SyntaxError) as exc:
        return MutantResult(mutant, "error", detail=f"could not write mutant: {exc}")
    target = f"{package}.{leaf}:make_app"
    try:
        outcome = replay_in_worker(target, traces, spec, [scratch, import_root], timeout)
    except WorkerError as exc:
        return MutantResult(mutant, "error", detail=str(exc))
    status = "survived" if outcome.matched == outcome.total else "killed"
    return MutantResult(mutant, status, outcome.matched, outcome.total)


def run_mutation(
    rebuild: str | Path,
    traces: str | Path,
    spec: str | Path | None = None,
    module: str | None = None,
    max_mutants: int | None = None,
    timeout: float = 120.0,
    jobs: int | None = None,
    seed: int = 0,
) -> MutationRun:
    """Score the recording against ``rebuild``. The unmutated rebuild must replay 100% first."""
    started = time.monotonic()
    path = Path(rebuild)
    module = module or module_name_for(path)
    leaf = module.rsplit(".", 1)[-1]
    source = path.read_text(encoding="utf-8")
    import_root = _import_root(path, module)
    jobs = max(jobs or os.cpu_count() or 1, 1)
    traces_arg = str(traces)
    spec_arg = str(spec) if spec is not None else None

    baseline = replay_in_worker(f"{module}:make_app", traces_arg, spec_arg, [import_root], timeout)
    if baseline.total == 0 or baseline.matched != baseline.total:
        raise RuntimeError(
            f"the unmutated rebuild replays {baseline.matched} / {baseline.total}; mutation scores need 100%"
        )

    everything = generate_mutants(source)
    chosen = select_mutants(everything, max_mutants, seed)
    with tempfile.TemporaryDirectory(prefix="archaeology-mutants-") as scratch_dir:
        scratch = Path(scratch_dir)

        def evaluate(mutant: Mutant) -> MutantResult:
            return _evaluate(mutant, source, leaf, scratch, import_root, traces_arg, spec_arg, timeout)

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(evaluate, chosen))
    return MutationRun(
        rebuild=str(path),
        module=module,
        traces=traces_arg,
        spec=spec_arg,
        generated=len(everything),
        baseline=baseline,
        results=results,
        wall_seconds=time.monotonic() - started,
        jobs=jobs,
        timeout=timeout,
        max_mutants=max_mutants,
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _percent(score: float | None) -> str:
    return "n/a" if score is None else f"{score * 100:.1f}%"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _survivor_sections(results: Sequence[MutantResult]) -> list[str]:
    survivors = [r for r in results if r.status == "survived"]
    if not survivors:
        return ["No mutant survived.", ""]
    lines: list[str] = []
    by_function: dict[str, list[MutantResult]] = {}
    for result in sorted(survivors, key=lambda r: (r.mutant.line, r.mutant.id)):
        by_function.setdefault(result.mutant.function, []).append(result)
    for function, rows in by_function.items():
        lines += [f"### `{function}`", "", "| Line | Mutant | Operator | Original | Mutated |", "| --- | --- | --- | --- | --- |"]
        for row in rows:
            m = row.mutant
            lines.append(
                f"| {m.line} | `{_cell(m.id)}` | {m.operator} | `{_cell(m.original)}` | `{_cell(m.mutated)}` |"
            )
        lines.append("")
    return lines


def _error_section(results: Sequence[MutantResult]) -> list[str]:
    errors = [r for r in results if r.status == "error"]
    if not errors:
        return []
    lines = ["## Errors", "", "Not counted in the score: the mutant failed to import, start or finish.", ""]
    lines += ["| Line | Mutant | Detail |", "| --- | --- | --- |"]
    lines += [f"| {r.mutant.line} | `{_cell(r.mutant.id)}` | {_cell(r.detail)} |" for r in errors]
    return lines + [""]


def render_markdown(run: MutationRun) -> str:
    """The mutation report as Markdown: the score, counts by operator and the survivors by function."""
    s = run.summary()
    evaluated = len(run.results)
    sampled = f" (sampled {evaluated} with --max-mutants {run.max_mutants})" if run.max_mutants is not None and run.max_mutants < run.generated else ""
    lines = [
        f"# Mutation report: {run.module}",
        "",
        f"**Mutation score: {_percent(s['score'])}** "
        f"(killed {s['killed']}, survived {s['survived']}, errors {s['errors']})",
        "",
        f"Mutants generated: {run.generated}; evaluated: {evaluated}{sampled}. "
        f"Errors are excluded from the score.",
        "",
        f"Baseline: the unmutated rebuild replays {run.baseline.matched} / {run.baseline.total} "
        f"exchanges equivalent.",
        "",
        f"Wall time: {run.wall_seconds:.1f} s with {run.jobs} jobs, timeout {run.timeout:g} s per mutant. "
        f"Rebuild: `{run.rebuild}`. Traces: `{run.traces}`. Spec: `{run.spec or 'none'}`.",
        "",
        "## By operator",
        "",
        "| Operator | Killed | Survived | Errors | Score |",
        "| --- | --- | --- | --- | --- |",
    ]
    for operator, counts in s["by_operator"].items():
        lines.append(
            f"| {operator} | {counts['killed']} | {counts['survived']} | {counts['errors']} | {_percent(counts['score'])} |"
        )
    lines += ["", "## Surviving mutants", "", "Grouped by the function they change.", ""]
    lines += _survivor_sections(run.results)
    lines += _error_section(run.results)
    return "\n".join(lines).rstrip() + "\n"


def to_dict(run: MutationRun) -> dict[str, Any]:
    """A JSON-serialisable form of the run, with every mutant's status."""
    return {
        "rebuild": run.rebuild,
        "module": run.module,
        "traces": run.traces,
        "spec": run.spec,
        "generated": run.generated,
        "evaluated": len(run.results),
        "max_mutants": run.max_mutants,
        "jobs": run.jobs,
        "timeout": run.timeout,
        "wall_seconds": round(run.wall_seconds, 2),
        "baseline": {"matched": run.baseline.matched, "total": run.baseline.total},
        "summary": run.summary(),
        "mutants": [
            {
                "id": r.mutant.id,
                "operator": r.mutant.operator,
                "line": r.mutant.line,
                "function": r.mutant.function,
                "original": r.mutant.original,
                "mutated": r.mutant.mutated,
                "status": r.status,
                "matched": r.matched,
                "total": r.total,
                "detail": r.detail,
            }
            for r in run.results
        ],
    }


# ---------------------------------------------------------------------------
# Worker entry point: one replay, one process
# ---------------------------------------------------------------------------


def _worker_main(argv: Sequence[str]) -> int:
    target, traces_path, spec_arg = argv
    traces = read_traces(traces_path)
    spec = None if spec_arg == "-" else load_spec(spec_arg)
    ignore = None if spec is None else (lambda request: volatile_paths_for(spec, request))
    system = load_target(target)
    try:
        report = replay(traces, system, ignore=ignore)
    finally:
        close = getattr(system, "close", None)
        if callable(close):
            close()
    print(json.dumps({"matched": report.matched, "total": report.total}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        sys.exit(_worker_main(sys.argv[2:]))
    raise SystemExit("run mutation scoring with `archaeology mutate`")
