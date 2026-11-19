"""Tests for the rebuild brief's flows, scenario runs, rule tables and summary."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest

from archaeology.brief import MAX_SCENARIO_RUNS, describe_id_format, generate_brief
from archaeology.infer import infer_spec
from archaeology.trace import Request, Response, Trace

_ids = iter(range(1, 10**9))


def make(method: str, path: str, params: dict[str, Any] | None = None, *, status: int = 200,
         body: Any = None) -> Trace:
    text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
    request = Request.build(method, path, query_string=urlencode(params or {}))
    response = Response(status=status, headers={"content-type": "application/json"}, body=text)
    return Trace(id=f"t-{next(_ids):06d}", ts="2026-11-18T00:00:00+00:00", request=request, response=response)


def in_scenario(trace: Trace, name: str, run: int, step: int, variables: dict[str, str]) -> Trace:
    trace.scenario = {"name": name, "run": run, "step": step, "vars": dict(variables)}
    return trace


def days(start: str, stop: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(stop)
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


DUE = date(2026, 1, 19)


def late_return_run(run: int, returned_on: str) -> list[Trace]:
    """Create a member, borrow a book, return it. Returns are refused after 2026-02-01.

    ``days_late`` grows one per day after a two-day grace period, so the scenario has a
    linear rule and a shape change, each at a known date.
    """
    member = f"m_{0x3FA9C2D1 + run:x}"
    loan = f"L-{0x04B7E2 + run:06x}"
    variables = {"returned_on": returned_on}
    late = (date.fromisoformat(returned_on) - DUE).days
    if returned_on <= "2026-02-01":
        ret = make("POST", f"/loans/{loan}/return", {"returned_on": returned_on}, status=200,
                   body={"status": "returned", "days_late": max(0, late - 2)})
    else:
        ret = make("POST", f"/loans/{loan}/return", {"returned_on": returned_on}, status=409,
                   body={"error": "LOAN_CLOSED"})
    return [
        in_scenario(make("POST", "/members", {"name": "Ada"}, status=201, body={"member_id": member}),
                    "late-return", run, 0, variables),
        in_scenario(make("POST", "/loans", {"member_id": member, "isbn": "9780000000011"}, status=201,
                         body={"loan_id": loan, "member_id": member}),
                    "late-return", run, 1, variables),
        in_scenario(ret, "late-return", run, 2, variables),
    ]


def late_return_traces() -> list[Trace]:
    traces: list[Trace] = []
    for run, day in enumerate(days("2026-01-10", "2026-02-20")):
        traces += late_return_run(run, day)
    return traces


@pytest.fixture(scope="module")
def traces() -> list[Trace]:
    return late_return_traces()


@pytest.fixture(scope="module")
def spec(traces) -> dict[str, Any]:
    return infer_spec(traces)


def _section(brief: str, heading: str) -> str:
    start = brief.index(heading)
    rest = brief[start + len(heading):]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


# --- identifier formats ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["m_3fa9c2d1", "m_77c0e1aa"], "`m_` followed by 8 lowercase hex characters"),
        (["L-04b7e2", "L-04b7e3"], "`L-` followed by 6 lowercase hex characters"),
        (["9780000000011", "9780000000028"], "13 digits"),
        (["ab12", "abcdef12345"], "4 to 11 lowercase hex characters"),
        (["123e4567-e89b-12d3-a456-426614174000"], "a UUID (8-4-4-4-12 hexadecimal characters)"),
        ([], "no values observed"),
    ],
)
def test_id_format_describes_prefix_length_and_class(values, expected):
    assert describe_id_format(values) == expected


def test_flows_section_lists_flows_and_id_formats(spec, traces):
    brief = generate_brief(spec, traces, system_name="Test Library")
    flows = _section(brief, "## Identifier flows")
    assert "member_id from POST /members is sent to POST /loans as param member_id" in flows
    assert "loan_id from POST /loans is sent to POST /loans/{id}/return as path" in flows
    assert "`m_` followed by 8 lowercase hex characters" in flows
    assert "`L-` followed by 6 lowercase hex characters" in flows
    assert "not compared" in flows and "only their format" in flows


# --- scenario runs ----------------------------------------------------------------------


def test_scenario_runs_include_every_step_in_order(spec, traces):
    brief = generate_brief(spec, traces)
    scenario = _section(brief, "## Recorded scenario runs")
    runs = scenario.split("#### Run ")[1:]
    assert runs, "no runs were rendered"
    assert len(runs) <= MAX_SCENARIO_RUNS
    for chunk in runs:
        first, second, third = (chunk.index(f"Step {n}:") for n in range(3))
        assert first < second < third
        assert "POST /members" in chunk[first:second]
        assert "POST /loans" in chunk[second:third]
        assert "/return" in chunk[third:]
        assert "-> 200" in chunk or "-> 409" in chunk


def test_scenario_runs_cover_both_sides_of_each_change_point(spec, traces):
    brief = generate_brief(spec, traces)
    scenario = _section(brief, "## Recorded scenario runs")
    # The linear rule changes at 2026-01-22 and the shape changes at 2026-02-02.
    for day in ("2026-01-21", "2026-01-22", "2026-02-01", "2026-02-02"):
        assert f"#### Run " in scenario and f"(returned_on={day})" in scenario, day


def test_scenario_runs_are_bounded_and_deterministic(spec, traces):
    first = generate_brief(spec, traces)
    assert first == generate_brief(spec, list(traces))
    scenario = _section(first, "## Recorded scenario runs")
    assert scenario.count("#### Run ") <= MAX_SCENARIO_RUNS


def test_endpoints_seen_only_in_scenarios_keep_examples(spec, traces):
    brief = generate_brief(spec, traces)
    examples = brief[brief.index("## Example exchanges"):]
    assert "### `POST /members`" in examples
    assert "### `POST /loans/{id}/return`" in examples


# --- rule tables and the summary ---------------------------------------------------------


def test_linear_rules_have_a_segment_table(spec, traces):
    linear = [b for b in spec["boundaries"] if b["kind"] == "linear"]
    assert linear, "the synthetic late-return rule should be piecewise linear"
    brief = generate_brief(spec, traces)
    tables = _section(brief, "### Piecewise-linear rules")
    assert "| slope per day |" in tables
    for finding in linear:
        for seg in finding["segments"]:
            assert f"| {seg['from']} | {seg['to']} | {seg['start']} | {seg['end']} | {seg['slope']} |" in tables


def test_rules_summary_lists_hidden_behaviour_and_contradicted_claims_before_endpoints():
    spec = {
        "trace_count": 0,
        "endpoints": [],
        "volatile_fields": {},
        "flows": [],
        "boundaries": [{"description": "GET /x: per is 1 up to 5", "kind": "step"}],
        "normalization": [{"description": "GET /x: q is case-insensitive"}],
        "claims": [{"id": "C1", "text": "Creating returns 200.", "verdict": "CONTRADICTED",
                    "mismatches": ["status: expected 200, observed 201"]}],
    }
    brief = generate_brief(spec, [])
    summary = brief[brief.index("## Rules the evidence shows"):brief.index("## Endpoints")]
    assert "- GET /x: per is 1 up to 5" in summary
    assert "- GET /x: q is case-insensitive" in summary
    assert "Creating returns 200." in summary and "observed 201" in summary


def test_pipeline_sections_present_for_empty_inputs():
    brief = generate_brief({"trace_count": 0, "endpoints": [], "volatile_fields": {}, "flows": [],
                            "boundaries": [], "normalization": [], "claims": []}, [])
    assert "- None detected." in brief
    assert "## Recorded scenario runs" in brief and "_No scenarios recorded._" in brief
