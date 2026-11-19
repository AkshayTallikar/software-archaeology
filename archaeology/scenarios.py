"""Scenarios: ordered, multi-step interactions whose later steps use earlier answers.

A workload's ``scenarios`` section lists scenario templates::

    {
      "name": "late-return",
      "vary": {"returned_on": {"date_range": ["2026-01-15", "2026-03-31", 1]}},
      "steps": [
        {"method": "POST", "path": "/members", "form": {"name": "Ada"},
         "capture": {"member": "member_id"}},
        {"method": "POST", "path": "/loans",
         "form": {"member_id": "{{member}}", "isbn": "9780000000011", "borrowed_on": "2026-01-05"},
         "capture": {"loan": "loan_id"}},
        {"method": "POST", "path": "/loans/{{loan}}/return", "form": {"returned_on": "{{returned_on}}"}}
      ]
    }

``vary`` works like a sweep: one run per combination of ``values``, ``range``
(``[start, stop_inclusive, step]``) or ``date_range`` (ISO dates, step in days).
The varied values are bound as variables. ``capture`` maps a variable name to a
dotted JSON path in the step's response body (``items.0.isbn``). ``{{name}}``
is replaced in the path, in ``raw_query``, and in query and form values. A
variable that was never bound (for example because the step that would have
captured it failed) is left as the literal placeholder, so the request is
still sent and the difference is visible in the traces. The exception is a
varied variable whose value is ``null`` in a run: a form or query value that is
exactly that variable's placeholder is left out of the request altogether.
"""

from __future__ import annotations

import itertools
import random
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Sequence

from .trace import Request, Response

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

Send = Callable[[Request], Response]


@dataclass
class ScenarioRun:
    """One concrete run of a scenario template: its bindings and its steps."""

    name: str
    run: int
    bindings: dict[str, str]
    steps: list[dict[str, Any]] = field(default_factory=list)
    omitted: frozenset[str] = frozenset()  # varied variables this run set to null: dropped, not sent

    def tag(self, step: int) -> dict[str, Any]:
        """The ``Trace.scenario`` tag for one step of this run.

        ``vars`` holds the run's varied values, so later stages can analyse how
        any step's response depends on them (a fee two steps after the date
        that caused it, for example).
        """
        return {"name": self.name, "run": self.run, "step": step, "vars": dict(self.bindings)}


# --- variable values -------------------------------------------------------


VARIANT_MARK = "~"


def scenario_family(name: str) -> str:
    """The template a scenario trace belongs to: ``late-return~refine1`` is a variant of ``late-return``.

    Refine adds variants of a template under a suffixed name so the original is untouched. Analysis
    groups a variant with its template, so the values it adds count toward the template's sweeps.
    """
    return name.split(VARIANT_MARK, 1)[0]


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _date_values(spec: Sequence[Any]) -> list[str]:
    start, stop, step = parse_date(spec[0]), parse_date(spec[1]), int(spec[2] if len(spec) > 2 else 1)
    if step <= 0:
        raise ValueError("date_range step must be a positive number of days")
    values = []
    day = start
    while day <= stop:
        values.append(day.isoformat())
        day += timedelta(days=step)
    return values


def vary_values(name: str, spec: dict[str, Any]) -> list[str | None]:
    """Every value a ``vary`` entry takes in a recording (``None`` means omit)."""
    if "values" in spec:
        return [None if v is None else str(v) for v in spec["values"]]
    if "range" in spec:
        start, stop, step = spec["range"]
        if step == 0:
            raise ValueError("range step must not be zero")
        stop_excl = stop + 1 if step > 0 else stop - 1
        return [str(i) for i in range(start, stop_excl, step)]
    if "date_range" in spec:
        return list(_date_values(spec["date_range"]))
    raise ValueError(f"vary parameter {name!r} needs 'values', 'range' or 'date_range'")


def sample_value(name: str, spec: dict[str, Any], rng: random.Random) -> str | None:
    """One value drawn from anywhere in a ``vary`` entry's domain, not only its grid."""
    if "values" in spec:
        value = rng.choice(spec["values"])
        return None if value is None else str(value)
    if "range" in spec:
        start, stop, _ = spec["range"]
        low, high = min(start, stop), max(start, stop)
        return str(rng.randint(low, high))
    if "date_range" in spec:
        start, stop = parse_date(spec["date_range"][0]), parse_date(spec["date_range"][1])
        return (start + timedelta(days=rng.randint(0, (stop - start).days))).isoformat()
    raise ValueError(f"vary parameter {name!r} needs 'values', 'range' or 'date_range'")


# --- expansion and sampling ------------------------------------------------


def expand_scenarios(workload: dict[str, Any], first_run: int = 0) -> list[ScenarioRun]:
    """One run per scenario per combination of its varied values, numbered globally."""
    runs: list[ScenarioRun] = []
    index = first_run
    for template in workload.get("scenarios", []):
        vary = template.get("vary", {})
        names = list(vary)
        for combo in itertools.product(*(vary_values(n, vary[n]) for n in names)):
            bindings = {n: v for n, v in zip(names, combo) if v is not None}
            omitted = frozenset(n for n, v in zip(names, combo) if v is None)
            runs.append(ScenarioRun(template["name"], index, bindings, list(template["steps"]), omitted))
            index += 1
    return runs


def sample_scenarios(
    workload: dict[str, Any], count: int, seed: int = 0, first_run: int = 0
) -> list[ScenarioRun]:
    """``count`` fresh runs: a scenario chosen at random, each varied value drawn at random."""
    templates = workload.get("scenarios", [])
    if not templates:
        return []
    rng = random.Random(seed)
    runs: list[ScenarioRun] = []
    for offset in range(count):
        template = rng.choice(templates)
        bindings: dict[str, str] = {}
        omitted: set[str] = set()
        for name, spec in template.get("vary", {}).items():
            value = sample_value(name, spec, rng)
            if value is None:
                omitted.add(name)
            else:
                bindings[name] = value
        runs.append(ScenarioRun(template["name"], first_run + offset, bindings, list(template["steps"]),
                                frozenset(omitted)))
    return runs


# --- rendering and capture -------------------------------------------------


def substitute(text: str, variables: dict[str, str]) -> str:
    """Replace ``{{name}}`` with its bound value; unbound placeholders stay as written."""
    return _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), m.group(0)), text)


def _is_omitted_placeholder(value: Any, omitted: frozenset[str]) -> bool:
    match = _PLACEHOLDER.fullmatch(str(value))
    return match is not None and match.group(1) in omitted


def render_step(
    step: dict[str, Any], variables: dict[str, str], omitted: frozenset[str] = frozenset()
) -> Request:
    """Turn one step spec into a concrete :class:`Request`.

    A form or query value that is exactly the placeholder of a variable in
    ``omitted`` (a varied variable that is null in this run) is left out.
    """
    from .recorder.exerciser import request_from_spec

    rendered: dict[str, Any] = {"method": step.get("method", "GET"), "path": substitute(step["path"], variables)}
    for key in ("query", "form"):
        if key in step:
            rendered[key] = {
                k: substitute(str(v), variables)
                for k, v in step[key].items()
                if v is not None and not _is_omitted_placeholder(v, omitted)
            }
    if "raw_query" in step:
        rendered["raw_query"] = substitute(step["raw_query"], variables)
    return request_from_spec(rendered)


def json_path_get(obj: Any, path: str) -> Any:
    """Follow a dotted path (``items.0.isbn``) into decoded JSON; ``None`` if absent."""
    current = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.lstrip("-").isdigit() and -len(current) <= int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def apply_captures(step: dict[str, Any], response: Response, variables: dict[str, str]) -> None:
    """Bind each ``capture`` variable that the response provides (scalars only)."""
    captures = step.get("capture") or {}
    if not captures:
        return
    body = response.json()
    for name, path in captures.items():
        value = json_path_get(body, path)
        if value is None or isinstance(value, (dict, list)):
            continue
        if isinstance(value, bool):
            variables[name] = "true" if value else "false"
        else:
            variables[name] = value if isinstance(value, str) else str(value)


def run_scenario(
    run: ScenarioRun,
    send: Send,
    on_exchange: Callable[[Request, Response, dict[str, Any]], None] | None = None,
) -> list[tuple[Request, Response]]:
    """Run every step in order against ``send``; returns the exchanges.

    ``on_exchange(request, response, tag)`` is called after each step with the
    step's ``Trace.scenario`` tag.
    """
    variables = dict(run.bindings)
    exchanges: list[tuple[Request, Response]] = []
    for index, step in enumerate(run.steps):
        request = render_step(step, variables, run.omitted)
        response = send(request)
        apply_captures(step, response, variables)
        exchanges.append((request, response))
        if on_exchange is not None:
            on_exchange(request, response, run.tag(index))
    return exchanges
