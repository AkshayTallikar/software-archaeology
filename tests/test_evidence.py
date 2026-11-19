"""Evidence on findings, claim support, and the evidence gaps a recording leaves."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from archaeology.brief import generate_brief
from archaeology.cli import DEMO_SYSTEMS, main
from archaeology.html_report import render_html_report
from archaeology.infer import detect_boundaries, detect_evidence_gaps, infer_spec, render_report
from archaeology.infer.boundaries import evidence_note
from archaeology.infer.claims import check_claims
from archaeology.trace import Request, Response, Trace, read_traces

ROOT = Path(__file__).resolve().parent.parent
_ids = iter(range(1, 10**9))


def make(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    status: int = 200,
    body: Any = None,
    scenario: dict[str, Any] | None = None,
) -> Trace:
    text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
    request = Request.build(method, path, query_string=urlencode(params or {}))
    response = Response(status=status, headers={"content-type": "application/json"}, body=text)
    return Trace(id=f"t-{next(_ids):06d}", ts="2026-11-19T00:00:00+00:00", request=request,
                 response=response, scenario=scenario)


def discount(qty_values: list[int], sku: str = "WIDGET", coupon: str = "") -> list[Trace]:
    """A bulk discount that is 0 up to qty 100 and 10 from 101, sampled at ``qty_values``."""
    params = {"sku": sku, "qty": ""}
    traces = []
    for q in qty_values:
        params["qty"] = str(q)
        body = {"qty": q, "bulk_discount_pct": 0 if q <= 100 else 10}
        traces.append(make("GET", "/quote", dict(params), body=body))
    return traces


def fine(sku: str = "WIDGET") -> list[Trace]:
    return discount(list(range(1, 201)), sku=sku)


def coarse(sku: str = "WIDGET") -> list[Trace]:
    return discount(list(range(1, 201, 5)), sku=sku)


def only_change(boundaries: list[dict[str, Any]], field: str) -> dict[str, Any]:
    matches = [b for b in boundaries if b["field"] == field]
    assert len(matches) == 1, [b["description"] for b in boundaries]
    return matches[0]


# --- evidence on boundary findings -------------------------------------------


def test_exact_step_edge_is_adjacent_and_counts_points() -> None:
    finding = only_change(detect_boundaries(fine()), "bulk_discount_pct")
    evidence = finding["evidence"]
    assert evidence["points"] == 200
    [change] = evidence["changes"]
    assert change["at"] == 101
    assert change["before"] == {"input": 100, "value": 0}
    assert change["after"] == {"input": 101, "value": 10}
    assert change["exact"] is True and change["unsampled"] == 0
    assert evidence_note(finding) == "200 points; change exact"


def test_coarse_step_edge_is_inexact_and_says_how_many_inputs_are_unsampled() -> None:
    finding = only_change(detect_boundaries(coarse()), "bulk_discount_pct")
    evidence = finding["evidence"]
    assert evidence["points"] == 40
    [change] = evidence["changes"]
    assert change["before"] == {"input": 96, "value": 0}
    assert change["after"] == {"input": 101, "value": 10}
    assert change["exact"] is False and change["unsampled"] == 4
    assert evidence_note(finding) == "40 points; change ±4"


def test_existing_finding_keys_are_unchanged() -> None:
    finding = only_change(detect_boundaries(fine()), "bulk_discount_pct")
    assert finding["kind"] == "step"
    assert finding["segments"][1]["from"] == 101
    assert finding["changes_at"] == [101]
    assert finding["contexts"] == 1


def test_clamp_start_is_its_last_echo_and_the_next_sample() -> None:
    traces = [make("GET", "/q", {"qty": str(q)}, body={"qty": min(q, 10)}) for q in range(1, 13)]
    finding = only_change(detect_boundaries(traces), "qty")
    assert finding["kind"] == "clamp"
    [change] = finding["evidence"]["changes"]
    assert change["at"] == 10
    assert change["before"] == {"input": 10, "value": 10}
    assert change["after"] == {"input": 11, "value": 10}
    assert change["exact"] is True


def test_linear_breakpoint_evidence_uses_the_samples_either_side() -> None:
    # Free up to 10, then 2 per unit: a slope change between samples 10 and 11.
    traces = [make("GET", "/fee", {"days": str(d)}, body={"fee": max(0, 2 * (d - 10))}) for d in range(1, 31)]
    finding = only_change(detect_boundaries(traces), "fee")
    assert finding["kind"] == "linear"
    [change] = finding["evidence"]["changes"]
    assert change["before"] == {"input": 10, "value": 0}
    assert change["after"] == {"input": 11, "value": 2}
    assert change["exact"] is True and finding["evidence"]["points"] == 30


def test_shape_change_evidence_is_per_change_point() -> None:
    traces = []
    for q in [1, 2, 3, 4, 10, 11, 12, 13, 14, 15]:
        body = {"ok": True, "qty": q} if q >= 10 else {"error": "SMALL"}
        traces.append(make("GET", "/s", {"qty": str(q)}, status=200 if q >= 10 else 400, body=body))
    shape = only_change(detect_boundaries(traces), "shape")
    [change] = shape["evidence"]["changes"]
    assert change["before"]["input"] == 4 and change["after"]["input"] == 10
    assert change["exact"] is False and change["unsampled"] == 5


def test_dedupe_keeps_the_evidence_of_the_smallest_sweep() -> None:
    dense = discount(list(range(1, 201)), sku="A")
    sparse = discount(list(range(1, 101)) + [101, 150, 200], sku="B")
    finding = only_change(detect_boundaries(dense + sparse), "bulk_discount_pct")
    assert finding["contexts"] == 2
    assert finding["evidence"]["points"] == 103
    assert finding["evidence"]["changes"][0]["exact"] is True


def test_evidence_note_for_findings_without_evidence_is_empty() -> None:
    assert evidence_note({"description": "x"}) == ""


# --- claim support -----------------------------------------------------------


def test_claim_support_counts_matching_and_contradicting_traces() -> None:
    traces = [
        make("GET", "/x", {"a": "1"}, status=200, body={"ok": True}),
        make("GET", "/x", {"a": "1"}, status=200, body={"ok": True}),
        make("GET", "/x", {"a": "1"}, status=500, body={"ok": False}),
        make("GET", "/y", {"a": "1"}, status=200),
    ]
    claim = {"id": "C1", "text": "x succeeds", "request": {"method": "GET", "path": "/x", "query": {"a": "1"}},
             "expect": {"status": 200}}
    [result] = check_claims([claim], traces)
    assert result["verdict"] == "CONFIRMED"  # the first matching trace agrees
    assert result["support"] == {"matching": 2, "contradicting": 1}
    assert result["request"] == claim["request"]


def test_unobserved_claim_has_zero_support() -> None:
    [result] = check_claims([{"id": "C2", "text": "z", "request": {"method": "GET", "path": "/z"}}], [])
    assert result["verdict"] == "UNOBSERVED"
    assert result["support"] == {"matching": 0, "contradicting": 0}


# --- evidence gaps: one test per kind ----------------------------------------


def gaps_of(kind: str, gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [g for g in gaps if g["kind"] == kind]


def test_inexact_change_gap_suggests_the_missing_sweep() -> None:
    traces = coarse()
    gaps = detect_evidence_gaps(traces, detect_boundaries(traces), [])
    [gap] = gaps_of("inexact_change", gaps)
    assert gap["where"] == "GET /quote (sku=WIDGET), qty"
    assert "between sampled 96 and 101" in gap["detail"]
    assert gap["suggestion"] == "add a sweep of qty from 96 to 101 on GET /quote (sku=WIDGET)"


def test_exact_change_is_not_a_gap() -> None:
    traces = fine()
    assert gaps_of("inexact_change", detect_evidence_gaps(traces, detect_boundaries(traces), [])) == []


def test_bracket_shared_by_several_contexts_is_reported_once() -> None:
    traces = discount(list(range(1, 201, 5)), sku="A") + discount(list(range(1, 201, 5)), sku="B")
    boundaries = detect_boundaries(traces)
    assert [b["contexts"] for b in boundaries if b["field"] == "bulk_discount_pct"] == [2]
    [gap] = gaps_of("inexact_change", detect_evidence_gaps(traces, boundaries, []))
    assert "1 more context" in gap["where"]


def few(values: list[str], name: str = "tier") -> list[Trace]:
    return [make("POST", "/members", {"name": "Ada", name: v}, status=201) for v in values]


def test_two_distinct_values_are_a_few_values_gap() -> None:
    traces = few(["gold", "standard", "gold"])
    [gap] = gaps_of("few_values", detect_evidence_gaps(traces, [], []))
    assert gap["where"] == "POST /members parameter tier"
    assert gap["detail"] == "only 'gold', 'standard' seen, across 3 requests"


def test_one_value_is_a_fixed_input_and_three_values_are_varied() -> None:
    assert gaps_of("few_values", detect_evidence_gaps(few(["gold", "gold"]), [], [])) == []
    traces = few(["gold", "standard", "premium"])
    assert gaps_of("few_values", detect_evidence_gaps(traces, [], [])) == []


def test_few_values_covers_scenario_variables() -> None:
    traces = [
        make("POST", "/members", {"name": "Ada"}, status=201,
             scenario={"name": "join", "run": run, "step": 0, "vars": {"tier": tier}})
        for run, tier in enumerate(["gold", "standard", "gold"])
    ]
    [gap] = gaps_of("few_values", detect_evidence_gaps(traces, [], []))
    assert gap["where"] == "scenario join variable tier"
    assert gap["suggestion"] == "add runs of scenario join with tier set to a third value"


def test_single_status_gap_needs_two_requests_and_one_status() -> None:
    same = [make("GET", "/books", {"page": str(p)}, status=200) for p in (1, 2)]
    lone = [make("GET", "/once", status=404)]
    mixed = [make("GET", "/mixed", {"n": "1"}, status=200), make("GET", "/mixed", {"n": "2"}, status=404)]
    gaps = gaps_of("single_status", detect_evidence_gaps(same + lone + mixed, [], []))
    assert [g["where"] for g in gaps] == ["GET /books"]
    assert gaps[0]["detail"] == "only HTTP 200 seen, across 2 requests"
    assert gaps[0]["suggestion"] == "add GET /books requests with an invalid value to see its other statuses"


def test_single_status_suggestion_names_an_unknown_id_and_a_missing_field() -> None:
    traces = [make("POST", "/loans/L-1234ab/return", status=200), make("POST", "/loans/L-1234ac/return", status=200)]
    [gap] = gaps_of("single_status", detect_evidence_gaps(traces, [], []))
    assert gap["suggestion"] == (
        "add POST /loans/{id}/return requests with an unknown id, a missing field or an invalid value "
        "to see its other statuses"
    )


def scenario_step(name: str, run: int, step: int, method: str, path: str, *, status: int = 200,
                  variables: dict[str, str] | None = None) -> Trace:
    scenario = {"name": name, "run": run, "step": step, "vars": dict(variables or {})}
    return make(method, path, status=status, scenario=scenario)


def test_failed_capture_of_an_earlier_answer() -> None:
    traces = [
        scenario_step("fees", 0, 0, "POST", "/members", status=201),
        scenario_step("fees", 0, 1, "POST", "/loans/{{loan}}/return"),
        scenario_step("fees", 1, 0, "POST", "/members", status=201),
        scenario_step("fees", 1, 1, "POST", "/loans/L-1/return", status=200),
    ]
    [gap] = gaps_of("failed_capture", detect_evidence_gaps(traces, [], []))
    assert gap["where"] == "scenario fees step 1 (POST /loans/{{loan}}/return)"
    assert gap["detail"] == "1 of 2 runs sent {{loan}} unbound, because an earlier step did not capture it"
    assert gap["suggestion"] == (
        "in scenario fees, make the earlier step that captures {{loan}} succeed in every run"
    )


def test_failed_capture_of_a_varied_value_that_was_null() -> None:
    # A run whose varied tier is null has no binding, so the literal placeholder is sent.
    traces = [
        scenario_step("join", 0, 0, "POST", "/members", variables={"tier": "gold"}, status=201),
        make("POST", "/members", {"tier": "{{tier}}"}, status=201,
             scenario={"name": "join", "run": 1, "step": 0, "vars": {}}),
        scenario_step("join", 2, 0, "POST", "/members", variables={"tier": "gold"}, status=201),
    ]
    [gap] = gaps_of("failed_capture", detect_evidence_gaps(traces, [], []))
    assert gap["detail"] == "1 of 3 runs sent {{tier}} unbound, though the scenario varies tier"
    assert gap["suggestion"] == "in scenario join, make a null tier omit the field instead of sending the placeholder"


def test_placeholder_requests_are_not_counted_as_single_status() -> None:
    traces = [scenario_step("fees", 0, 1, "GET", "/members/{{member}}", status=404),
              scenario_step("fees", 1, 1, "GET", "/members/{{member}}", status=404)]
    assert gaps_of("single_status", detect_evidence_gaps(traces, [], [])) == []


def test_unobserved_claim_gap_suggests_the_request_to_add() -> None:
    claims = [{"id": "C9", "text": "Quotes are cached",
               "request": {"method": "GET", "path": "/quote", "query": {"sku": "WIDGET", "qty": "3"}},
               "verdict": "UNOBSERVED"}]
    [gap] = gaps_of("unobserved_claim", detect_evidence_gaps([], [], claims))
    assert gap["where"] == "claim C9"
    assert gap["suggestion"] == "add a request GET /quote?sku=WIDGET&qty=3 to the workload"


def test_scenario_claim_gap_names_the_run_and_step() -> None:
    claims = [{"id": "C8", "text": "t", "verdict": "UNOBSERVED",
               "request": {"scenario": "late-return", "step": 2, "vars": {"returned_on": "2026-02-01"}}}]
    [gap] = gaps_of("unobserved_claim", detect_evidence_gaps([], [], claims))
    assert gap["suggestion"] == (
        "add a run of scenario late-return that reaches step 2 with returned_on=2026-02-01 to the workload"
    )


# --- spec, report, brief and page --------------------------------------------


def test_infer_spec_carries_evidence_gaps_and_claim_support() -> None:
    spec = infer_spec(coarse(), [{"id": "C1", "text": "t", "request": {"method": "GET", "path": "/none"}}])
    assert {g["kind"] for g in spec["evidence_gaps"]} >= {"inexact_change", "unobserved_claim"}
    assert spec["claims"][0]["support"] == {"matching": 0, "contradicting": 0}


def test_report_and_brief_render_the_thin_evidence_section() -> None:
    traces = coarse()
    spec = infer_spec(traces, [])
    report = render_report(spec)
    assert "## Thin evidence" in report
    assert "| inexact_change | GET /quote (sku=WIDGET), qty |" in report
    assert "(40 points; change ±4)" in report
    brief = generate_brief(spec, traces, system_name="Quote")
    assert "## Thin evidence: be careful here" in brief
    assert "- GET /quote (sku=WIDGET), qty: bulk_discount_pct changes somewhere between sampled 96 and 101" in brief


def test_exact_recording_has_no_inexact_gap_in_the_brief() -> None:
    traces = fine()
    spec = infer_spec(traces, [])
    brief = generate_brief(spec, traces, system_name="Quote")
    assert "## Thin evidence: be careful here" in brief
    assert "changes somewhere between" not in brief


def test_no_gaps_says_none_detected() -> None:
    traces = few(["gold", "standard", "premium"]) + [
        make("POST", "/members", {"name": "Ada"}, status=201),
        make("POST", "/members", {"name": "Ben"}, status=400),
        make("POST", "/members", {"name": "Cy"}, status=201),
    ]
    spec = infer_spec(traces, [])
    assert spec["evidence_gaps"] == []
    assert "- None detected." in generate_brief(spec, traces, system_name="Members")
    assert "_None detected._" in render_report(spec).split("## Thin evidence", 1)[1]


def test_html_report_lists_gaps_and_point_counts() -> None:
    traces = coarse()
    page = render_html_report(infer_spec(traces, []), traces, title="Quote")
    assert "<h2>Thin evidence</h2>" in page
    assert "suggested workload addition" in page
    assert "40 points; change ±4" in page


def test_specs_from_before_evidence_gaps_still_render() -> None:
    traces = coarse()
    old = infer_spec(traces, [])
    del old["evidence_gaps"]
    for finding in old["boundaries"]:
        finding.pop("evidence", None)
    assert "Not computed for this spec" in render_html_report(old, traces, title="Quote")
    assert "## Thin evidence: be careful here" not in generate_brief(old, traces, system_name="Quote")
    assert "Not computed for this spec" in render_report(old)


# --- pipelines on the bundled systems ----------------------------------------


@pytest.fixture(scope="module")
def billing_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("billing")
    system = DEMO_SYSTEMS["billing"]
    traces = out / "traces.jsonl"
    assert main(["record", "--target", system.reference, "--workload", system.workload, "--out", str(traces)]) == 0
    assert main(["infer", "--traces", str(traces), "--claims", system.claims,
                 "--spec", str(out / "spec.json"), "--report", str(out / "report.md")]) == 0
    return out


def test_billing_recording_has_thin_evidence_in_every_output(billing_run: Path) -> None:
    traces = read_traces(billing_run / "traces.jsonl")
    spec = json.loads((billing_run / "spec.json").read_text(encoding="utf-8"))
    kinds = {g["kind"] for g in spec["evidence_gaps"]}
    assert "inexact_change" in kinds  # the discount edge is sampled every 5 units in some contexts
    assert "few_values" in kinds  # coupon is tried with two codes only
    assert "single_status" in kinds  # the tier endpoint only ever answers 200 in the recording
    assert "failed_capture" not in kinds and "unobserved_claim" not in kinds
    assert all(b["evidence"]["points"] >= 8 for b in spec["boundaries"])
    report = (billing_run / "report.md").read_text(encoding="utf-8")
    assert "## Thin evidence" in report
    assert "_None detected._" not in report.split("## Thin evidence", 1)[1].split("## ", 1)[0]
    page = render_html_report(spec, traces, title="ACME Billing")
    assert "Thin evidence" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_library_recording_flags_its_failed_captures_and_fixed_tiers(tmp_path: Path) -> None:
    system = DEMO_SYSTEMS["library"]
    traces_path = tmp_path / "traces.jsonl"
    assert main(["record", "--target", system.reference, "--workload", system.workload,
                 "--out", str(traces_path)]) == 0
    spec = infer_spec(read_traces(traces_path), json.loads((ROOT / system.claims).read_text(encoding="utf-8")))
    failed = [g for g in spec["evidence_gaps"] if g["kind"] == "failed_capture"]
    wheres = {g["where"] for g in failed}
    # a null tier is omitted, so join step 0 no longer sends the literal {{tier}}
    assert "scenario join step 0 (POST /members)" not in wheres
    assert not any("{{tier}}" in g["where"] + g["detail"] for g in failed)
    # fees-block step 4 is the cleanup return of the second loan, unbound in the runs where that borrow is refused
    assert "scenario fees-block step 4 (POST /loans/{{loan2}}/return)" in wheres
    assert any("tier" in g["where"] for g in gaps_of("few_values", spec["evidence_gaps"]))
    assert any(g["where"] == "GET /books" for g in gaps_of("single_status", spec["evidence_gaps"]))
