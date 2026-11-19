"""Mutation testing: operators, skip rules, stable ids, and end-to-end scoring on a tiny app.

The end-to-end test uses a small WSGI rebuild and a recording of it, so it runs
in seconds. The real rebuilds are scored by ``archaeology mutate``, not here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from archaeology.mutate import (
    OPERATORS,
    REPO_ROOT,
    MutantResult,
    Mutant,
    generate_mutants,
    module_name_for,
    mutate_source,
    render_markdown,
    run_mutation,
    select_mutants,
    summarise,
    to_dict,
)
from archaeology.trace import Request, Trace, write_traces
from archaeology.wsgi import call_wsgi

OPERATOR_SOURCE = '''
TOTAL = "10.00"
RATE = 0.25


def price(qty, coupon=None):
    """Doc with 42 in it."""
    if qty > 100 and coupon == "X":
        return 1
    if qty == 0:
        return 0
    return qty * 2
'''


def _mutated_lines(source: str, mutant_id: str) -> list[str]:
    return ast.unparse(ast.parse(mutate_source(source, mutant_id))).splitlines()


def _by(mutants: list[Mutant], operator: str) -> list[Mutant]:
    return [m for m in mutants if m.operator == operator]


def test_each_operator_fires_on_its_construct() -> None:
    mutants = generate_mutants(OPERATOR_SOURCE)
    assert {m.operator for m in mutants} == set(OPERATORS)

    money = {(m.original, m.mutated) for m in _by(mutants, "money-literal")}
    assert ("10.00", "10.01") in money and ("10.00", "9.99") in money
    assert ("0.25", "0.26") in money and ("0.25", "0.24") in money

    comparisons = {m.original: m.mutated for m in _by(mutants, "comparison")}
    assert comparisons["qty > 100"] == "qty >= 100"
    assert comparisons['qty == 0'] == "qty != 0"

    booleans = _by(mutants, "boolean")
    assert [(m.original, m.mutated) for m in booleans] == [("qty > 100 and coupon == 'X'", "qty > 100 or coupon == 'X'")]

    ints = {m.mutated for m in _by(mutants, "int-literal") if m.line == 8}
    assert ints == {"101", "99"}


def test_money_mutants_move_by_one_unit_in_the_last_place() -> None:
    source = 'A = "10.00"\nB = 0.5\nC = "7"\nD = "x.y"\n'
    money = _by(generate_mutants(source), "money-literal")
    pairs = sorted((m.line, m.mutated) for m in money)
    assert pairs == [(1, "10.01"), (1, "9.99"), (2, "0.4"), (2, "0.6")]


def test_skips_docstrings_bare_expressions_annotations_and_booleans() -> None:
    source = (
        'def f(a: 7) -> 9:\n'
        '    """Docstring 5."""\n'
        '    42\n'
        '    flag = True\n'
        '    return a\n'
    )
    mutants = generate_mutants(source)
    assert mutants == []


def test_status_codes_are_mutated_as_behaviour() -> None:
    source = 'def handler(resp):\n    return resp(status_code=404)\n'
    ints = {m.mutated for m in _by(generate_mutants(source), "int-literal")}
    assert ints == {"405", "403"}


def test_ids_are_stable_and_unique() -> None:
    first = generate_mutants(OPERATOR_SOURCE)
    second = generate_mutants(OPERATOR_SOURCE)
    assert [m.id for m in first] == [m.id for m in second]
    assert len({m.id for m in first}) == len(first)
    assert [mutate_source(OPERATOR_SOURCE, m.id) for m in first] == [
        mutate_source(OPERATOR_SOURCE, m.id) for m in second
    ]


def test_each_mutant_changes_exactly_one_place() -> None:
    original = ast.unparse(ast.parse(OPERATOR_SOURCE)).splitlines()
    for mutant in generate_mutants(OPERATOR_SOURCE):
        if mutant.operator == "guard-order":  # swapping two statements changes two lines by design
            continue
        mutated = _mutated_lines(OPERATOR_SOURCE, mutant.id)
        assert len(mutated) == len(original), mutant.id
        differing = [i for i, (a, b) in enumerate(zip(original, mutated)) if a != b]
        assert len(differing) == 1, (mutant.id, differing)


def test_guard_order_swaps_two_consecutive_return_guards() -> None:
    source = (
        "def check(a, b):\n"
        "    if a > 1:\n"
        "        return 1\n"
        "    if b < 2:\n"
        "        return 2\n"
        "    x = 3\n"
        "    if a == b:\n"
        "        return 4\n"
        "    return x\n"
    )
    guards = _by(generate_mutants(source), "guard-order")
    assert len(guards) == 1
    assert guards[0].function == "check"
    assert guards[0].line == 2
    body = ast.parse(mutate_source(source, guards[0].id)).body[0].body  # type: ignore[attr-defined]
    assert ast.unparse(body[0].test) == "b < 2"
    assert ast.unparse(body[1].test) == "a > 1"


def test_guard_order_ignores_guards_with_else_or_other_statements() -> None:
    source = (
        "def check(a, b):\n"
        "    if a > 1:\n"
        "        return 1\n"
        "    else:\n"
        "        pass\n"
        "    if b < 2:\n"
        "        return 2\n"
        "    if a == b:\n"
        "        x = 4\n"
        "    return x\n"
    )
    assert _by(generate_mutants(source), "guard-order") == []


def test_mutate_source_rejects_an_unknown_id() -> None:
    with pytest.raises(KeyError):
        mutate_source(OPERATOR_SOURCE, "L999:C0:nope")


def test_select_mutants_is_a_repeatable_sample_in_source_order() -> None:
    mutants = generate_mutants(OPERATOR_SOURCE)
    picked = select_mutants(mutants, 4, seed=3)
    assert len(picked) == 4
    assert picked == select_mutants(mutants, 4, seed=3)
    order = [mutants.index(m) for m in picked]
    assert order == sorted(order)
    assert select_mutants(mutants, None) == mutants


def test_module_name_follows_the_path_under_the_working_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    assert module_name_for("modern_app/main.py") == "modern_app.main"
    assert module_name_for(REPO_ROOT / "modern_library" / "main.py") == "modern_library.main"


def test_summary_scores_killed_over_detected_and_excludes_errors() -> None:
    mutant = Mutant("L1:C0:int-literal+1", "int-literal", 1, "<module>", "1", "2")
    results = [
        MutantResult(mutant, "killed"),
        MutantResult(mutant, "killed"),
        MutantResult(mutant, "survived"),
        MutantResult(mutant, "error", detail="timeout"),
    ]
    summary = summarise(results)
    assert (summary["killed"], summary["survived"], summary["errors"]) == (2, 1, 1)
    assert summary["score"] == pytest.approx(2 / 3)
    assert summarise([MutantResult(mutant, "error")])["score"] is None


# ---------------------------------------------------------------------------
# End to end: a tiny WSGI rebuild with a recording that pins most, not all, of it
# ---------------------------------------------------------------------------

TINY_REBUILD = '''
from decimal import Decimal
from urllib.parse import parse_qs

UNIT_PRICE = "2.50"
DEFAULT_QTY = "1"
BULK_FROM = 100
BULK_PERCENT = 10


def make_app():
    def app(environ, start_response):
        query = parse_qs(environ.get("QUERY_STRING", ""))
        qty = int(query.get("qty", [DEFAULT_QTY])[0])
        total = Decimal(UNIT_PRICE) * qty
        if qty > BULK_FROM:
            total = total * (100 - BULK_PERCENT) / 100
        body = f"{total:.2f}".encode("utf-8")
        start_response("200 OK", [("content-type", "text/plain"), ("content-length", str(len(body)))])
        return [body]

    return app
'''

# The recording never asks for exactly 100 units or for a bulk order, so the
# bulk boundary is not pinned: its mutants survive.
RECORDED_QUERIES = ["", "qty=3", "qty=7", "qty=12"]


@pytest.fixture()
def tiny(tmp_path: Path) -> tuple[Path, Path]:
    rebuild = tmp_path / "tiny_rebuild.py"
    rebuild.write_text(TINY_REBUILD, encoding="utf-8")
    namespace: dict[str, object] = {}
    exec(compile(TINY_REBUILD, str(rebuild), "exec"), namespace)  # the reference is the same code
    reference = namespace["make_app"]()  # type: ignore[operator]
    traces = []
    for index, query in enumerate(RECORDED_QUERIES):
        request = Request.build("GET", "/price", query)
        response = call_wsgi(reference, request)
        traces.append(Trace(id=f"t-{index}", ts="", request=request, response=response))
    traces_path = tmp_path / "traces.jsonl"
    write_traces(traces_path, traces)
    return rebuild, traces_path


def test_recording_kills_real_changes_and_misses_the_untested_boundary(tiny: tuple[Path, Path]) -> None:
    rebuild, traces = tiny
    run = run_mutation(rebuild, traces, module="tiny_rebuild", timeout=60, jobs=4)

    assert run.baseline.matched == run.baseline.total == len(RECORDED_QUERIES)
    assert run.summary()["errors"] == 0, [r.detail for r in run.results if r.status == "error"]
    assert run.generated == len(generate_mutants(TINY_REBUILD))

    price_line = next(i for i, line in enumerate(TINY_REBUILD.splitlines(), 1) if "UNIT_PRICE = " in line)
    bulk_line = next(i for i, line in enumerate(TINY_REBUILD.splitlines(), 1) if "if qty > BULK_FROM" in line)

    status = {(r.mutant.line, r.mutant.original, r.mutant.mutated): r.status for r in run.results}
    assert status[(price_line, "2.50", "2.51")] == "killed"
    assert status[(price_line, "2.50", "2.49")] == "killed"
    assert status[(bulk_line, "qty > BULK_FROM", "qty >= BULK_FROM")] == "survived"

    summary = run.summary()
    assert summary["killed"] > 0 and summary["survived"] > 0
    assert 0 < summary["score"] < 1

    report = render_markdown(run)
    assert "Mutation score" in report
    assert "`qty > BULK_FROM`" in report
    assert json.loads(json.dumps(to_dict(run)))["summary"]["killed"] == summary["killed"]
