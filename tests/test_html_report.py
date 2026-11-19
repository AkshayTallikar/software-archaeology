"""Tests for the self-contained HTML evidence report, built from synthetic traces.

The Pinecrest integration test at the end runs record, infer, brief and report
on the Node.js sample; it is skipped when node is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from archaeology.brief import generate_brief
from archaeology.cli import main
from archaeology.html_report import render_html_report
from archaeology.infer import infer_spec
from archaeology.infer.spec import save_spec
from archaeology.trace import Request, Response, Trace, write_traces

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "legacy_library"

_ids = iter(range(1, 10**9))


def days(start: str, stop: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(stop)
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


def in_scenario(trace: Trace, name: str, run: int, step: int, variables: dict[str, str]) -> Trace:
    trace.scenario = {"name": name, "run": run, "step": step, "vars": dict(variables)}
    return trace


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



def make(method: str, path: str, params: dict[str, Any] | None = None, *, status: int = 200,
         body: Any = None) -> Trace:
    text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
    request = Request.build(method, path, query_string=urlencode(params or {}))
    response = Response(status=status, headers={"content-type": "application/json"}, body=text)
    return Trace(id=f"t-{next(_ids):06d}", ts="2026-11-18T00:00:00+00:00", request=request, response=response)


def quote_traces() -> list[Trace]:
    """A step: the discount is 0 up to qty 100 and 10 from 101 (a stateless sweep)."""
    return [
        make("GET", "/quote", {"sku": "WIDGET", "qty": q},
             body={"sku": "WIDGET", "qty": q, "bulk_discount_pct": 0 if q <= 100 else 10})
        for q in range(1, 201, 5)
    ]


def tier_traces(bad: str = "<img onerror=x>") -> list[Trace]:
    """A categorical step whose value is attacker-shaped text."""
    return [
        make("GET", "/tier", {"qty": q}, body={"tier": bad if q <= 50 else "gold"})
        for q in range(1, 101, 2)
    ]


@pytest.fixture(scope="module")
def traces() -> list[Trace]:
    return quote_traces() + late_return_traces()


@pytest.fixture(scope="module")
def spec(traces):
    return infer_spec(traces)


def _count_svg(html: str) -> int:
    return html.count("<svg")


# --- self-contained ---------------------------------------------------------------


def test_report_is_self_contained(spec, traces):
    page = render_html_report(spec, traces, title="Test Library")
    assert page.startswith("<!doctype html>")
    assert "<script" not in page
    assert "<link" not in page
    assert "@import" not in page
    assert "url(" not in page
    assert "http://" not in page and "https://" not in page
    assert "prefers-color-scheme: dark" in page


def test_report_is_deterministic(spec, traces):
    assert render_html_report(spec, traces) == render_html_report(spec, list(traces))


# --- charts -----------------------------------------------------------------------


def test_one_svg_per_chartable_finding(spec, traces):
    kinds = {b["kind"] for b in spec["boundaries"]}
    assert {"step", "linear", "shape"} <= kinds
    chartable = [b for b in spec["boundaries"] if b["kind"] in ("step", "clamp", "linear", "shape")]
    page = render_html_report(spec, traces)
    assert _count_svg(page) == len(chartable)


def test_linear_chart_marks_change_points_and_draws_the_line(spec, traces):
    page = render_html_report(spec, traces)
    assert 'class="chg"' in page  # dashed change-point line
    assert 'class="line"' in page  # polyline through the recomputed points


def test_shape_findings_get_coloured_bands_with_legend(spec, traces):
    page = render_html_report(spec, traces)
    assert re.search(r'<rect class="c\d"', page)
    assert '<ul class="legend">' in page


def test_recorded_values_are_escaped(traces):
    hostile = tier_traces()
    spec = infer_spec(hostile)
    page = render_html_report(spec, hostile)
    assert "<img onerror=x>" not in page
    assert "&lt;img onerror=x&gt;" in page


def test_chart_series_are_thinned_for_long_sweeps():
    many = [make("GET", "/quote", {"sku": "W", "qty": q},
                 body={"sku": "W", "qty": q, "bulk_discount_pct": 0 if q <= 1000 else 10})
            for q in range(1, 2001)]
    spec = infer_spec(many)
    page = render_html_report(spec, many)
    assert len(page) < 60_000
    assert page.count('class="pt"') == 0  # too many points for dots


# --- sections ----------------------------------------------------------------------


def test_documentation_table_colours_verdicts(spec, traces):
    claims = [
        {"id": "C1", "text": "Creating returns 200.", "verdict": "CONFIRMED", "mismatches": [], "observed": {"status": 200}},
        {"id": "C2", "text": "Late fee is 1.", "verdict": "CONTRADICTED", "mismatches": ["fee: expected 1, observed 0"], "observed": None},
        {"id": "C3", "text": "Unseen.", "verdict": "UNOBSERVED", "mismatches": [], "observed": None},
    ]
    page = render_html_report({**spec, "claims": claims}, traces)
    assert '<span class="badge ok">CONFIRMED</span>' in page
    assert '<span class="badge bad">CONTRADICTED</span>' in page
    assert '<span class="badge grey">UNOBSERVED</span>' in page
    assert "fee: expected 1, observed 0" in page


def test_identifier_flows_and_volatile_fields_sections(spec, traces):
    page = render_html_report(spec, traces)
    assert "<h2>Identifier flows</h2>" in page
    assert "`m_` followed by 8 lowercase hex characters" in page
    assert "<h2>Volatile fields</h2>" in page


def test_replay_and_probe_sections_appear_when_given(spec, traces):
    bare = render_html_report(spec, traces)
    assert "<h2>Replay</h2>" not in bare and "<h2>Probe</h2>" not in bare

    replay = {"total": 10, "matched": 9, "equivalence": 0.9, "remapped": 3,
              "mismatches": [{"trace_id": "t-1", "request": "GET /a", "differences": ["status: expected 200, got 500"]}]}
    probe = {"total": 100, "scenario_steps": 20, "agreement": 0.99,
             "divergences": [{"request": "GET /a", "query_or_body": "x=1", "scenario": None, "differences": ["body differs"]}]}
    page = render_html_report(spec, traces, replay=replay, probe=probe)
    assert "<h2>Replay</h2>" in page and "<h2>Probe</h2>" in page
    assert "9 / 10 responses equivalent (90.0%)" in page
    assert "Identifiers remapped: 3" in page
    assert "99 / 100 fresh exchanges agree" in page
    assert "Probe 1 divergences" in page


def test_divergences_are_truncated_to_twenty(spec, traces):
    mismatches = [
        {"trace_id": f"t-{i}", "request": f"GET /r{i}", "differences": ["status"]} for i in range(25)
    ]
    page = render_html_report(spec, traces, replay={"total": 25, "matched": 0, "equivalence": 0.0,
                                                    "mismatches": mismatches})
    assert "GET /r19" in page
    assert "GET /r20" not in page
    assert "... and 5 more" in page


def test_header_shows_counts_and_badges(spec, traces):
    page = render_html_report(spec, traces, title="Pinecrest Library",
                              replay={"total": 4, "matched": 4, "equivalence": 1.0})
    assert "<h1>Pinecrest Library</h1>" in page
    assert f"{spec['trace_count']} recorded exchanges" in page
    assert f"{len(spec['endpoints'])} endpoints" in page
    assert "Replay 100.0% equivalent" in page


# --- CLI ----------------------------------------------------------------------------


def test_cli_report_writes_the_page(tmp_path, traces):
    spec_path = tmp_path / "spec.json"
    traces_path = tmp_path / "traces.jsonl"
    out = tmp_path / "report.html"
    save_spec(infer_spec(traces), spec_path)
    write_traces(traces_path, traces)
    assert main(["report", "--spec", str(spec_path), "--traces", str(traces_path),
                 "--title", "CLI Test", "--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "<h1>CLI Test</h1>" in page and "<svg" in page


# --- Pinecrest integration ----------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_pinecrest_pipeline_renders_brief_and_report(tmp_path):
    from archaeology.recorder import ListSink, exercise_target
    from archaeology.recorder.exerciser import load_workload
    from archaeology.replay import ProcessTarget
    from archaeology.infer.claims import load_claims

    workload = load_workload(LIBRARY / "workload.json")
    sink = ListSink()
    with ProcessTarget(["node", str(LIBRARY / "server.js")]) as target:
        exercise_target(target, workload, sink)
    spec = infer_spec(sink, load_claims(LIBRARY / "claims.json"))

    brief = generate_brief(spec, sink, system_name="Pinecrest Library")
    assert "## Identifier flows" in brief
    assert "## Recorded scenario runs" in brief
    assert "### Scenario `late-return`" in brief

    page = render_html_report(spec, sink, title="Pinecrest Library")
    assert "<script" not in page and "https://" not in page
    assert _count_svg(page) >= 1
