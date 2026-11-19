"""Refine: each gap kind's addition generator, and the loop that closes gaps against a system."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import pytest

from archaeology.infer import detect_boundaries, detect_evidence_gaps, infer_spec
from archaeology.recorder import ListSink, exercise_target, expand_workload, load_workload
from archaeology.refine import (
    Context,
    addition_for_gap,
    empty_addition,
    gap_key,
    inexact_addition,
    refine,
    render_refine_log,
    unknown_ids,
)
from archaeology.replay import ProcessTarget
from archaeology.scenarios import expand_scenarios, scenario_family
from archaeology.trace import Request, Response, Trace
from archaeology.wsgi import call_wsgi

ROOT = Path(__file__).resolve().parent.parent
_ids = iter(range(1, 10**9))


def make(method: str, path: str, params: dict[str, Any] | None = None, *, status: int = 200,
         body: Any = None, scenario: dict[str, Any] | None = None) -> Trace:
    text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
    request = Request.build(method, path, query_string=urlencode(params or {}))
    response = Response(status=status, headers={"content-type": "application/json"}, body=text)
    return Trace(id=f"t-{next(_ids):06d}", ts="2026-11-19T00:00:00+00:00", request=request,
                 response=response, scenario=scenario)


def ctx_for(traces: list[Trace] | None = None, workload: dict[str, Any] | None = None,
            claims: list[dict[str, Any]] | None = None, spec: dict[str, Any] | None = None) -> Context:
    traces = traces or []
    return Context(traces=traces, spec=spec or infer_spec(traces, claims or []),
                   workload=workload or {}, claims=claims or [])


def gap_of(kind: str, gaps: list[dict[str, Any]]) -> dict[str, Any]:
    [gap] = [g for g in gaps if g["kind"] == kind]
    return gap


# --- gap data ----------------------------------------------------------------


def coarse_discount(sku: str = "WIDGET") -> list[Trace]:
    """A bulk discount that starts at qty 101, sampled every fifth unit."""
    return [make("GET", "/quote", {"sku": sku, "qty": str(q)},
                 body={"qty": q, "bulk_discount_pct": 0 if q <= 100 else 10}) for q in range(1, 201, 5)]


def test_inexact_gap_carries_its_bracket_and_every_context_that_shares_it() -> None:
    traces = coarse_discount("A") + coarse_discount("B")
    gaps = detect_evidence_gaps(traces, detect_boundaries(traces), [])
    gap = gap_of("inexact_change", gaps)
    assert gap["data"]["lo"] == "96" and gap["data"]["hi"] == "101"
    assert gap["data"]["param"] == "qty" and gap["data"]["axis"] == "int"
    assert gap["data"]["location"] == "query" and gap["data"]["path"] == "/quote"
    assert sorted(c["sku"] for c in gap["data"]["contexts"]) == ["A", "B"]


def test_gap_text_is_unchanged_by_its_data() -> None:
    traces = coarse_discount()
    [gap] = [g for g in detect_evidence_gaps(traces, detect_boundaries(traces), []) if g["kind"] == "inexact_change"]
    assert gap["where"] == "GET /quote (sku=WIDGET), qty"
    assert gap["suggestion"] == "add a sweep of qty from 96 to 101 on GET /quote (sku=WIDGET)"


def test_every_gap_has_machine_readable_data() -> None:
    traces = [make("GET", "/items/i-000001"), make("GET", "/items/i-000002")]
    gaps = detect_evidence_gaps(traces, [], [])
    assert gaps and all(isinstance(g["data"], dict) for g in gaps)


# --- inexact changes ---------------------------------------------------------


def test_inexact_addition_sweeps_every_unsampled_input_in_its_context() -> None:
    gap = {"kind": "inexact_change", "where": "w", "detail": "d", "suggestion": "s", "data": {
        "endpoint": "GET /quote", "method": "GET", "path": "/quote", "location": "query", "param": "qty",
        "field": "bulk_discount_pct", "axis": "int", "lo": "100", "hi": "105", "scenario": None,
        "step": None, "contexts": [{"sku": "WIDGET"}]}}
    addition, text = addition_for_gap(gap, ctx_for())
    assert addition is not None and "101, 102, 103, 104" in text
    requests = expand_workload(addition)
    assert [r.query_string for r in requests] == [
        "sku=WIDGET&qty=101", "sku=WIDGET&qty=102", "sku=WIDGET&qty=103", "sku=WIDGET&qty=104"]


def test_inexact_addition_over_dates_steps_one_day_at_a_time() -> None:
    addition, _ = inexact_addition({"param": "returned_on", "axis": "date", "lo": "2026-01-05",
                                    "hi": "2026-01-09", "scenario": "late-return", "contexts": [{}]},
                                   ctx_for(workload={"scenarios": [
                                       {"name": "late-return", "vary": {"returned_on": {"date_range": [
                                           "2026-01-01", "2026-01-31", 1]}}, "steps": []}]}))
    [variant] = addition["scenarios"]
    assert variant["vary"]["returned_on"] == {"values": ["2026-01-06", "2026-01-07", "2026-01-08"]}


def test_scenario_inexact_addition_is_a_suffixed_copy_and_leaves_the_template_alone() -> None:
    template = {"name": "late-return", "vary": {"returned_on": {"date_range": ["2026-01-01", "2026-01-31", 1]},
                                                "tier": {"values": ["standard", None]}},
                "steps": [{"method": "POST", "path": "/loans"}]}
    workload = {"scenarios": [template]}
    before = copy.deepcopy(workload)
    gap = {"kind": "inexact_change", "where": "w", "detail": "d", "suggestion": "s", "data": {
        "endpoint": "POST /loans", "param": "returned_on", "axis": "date", "lo": "2026-01-05",
        "hi": "2026-01-09", "scenario": "late-return", "step": 0, "field": "fee", "contexts": [{"tier": "standard"}]}}
    addition, _ = addition_for_gap(gap, ctx_for(workload=workload))
    [variant] = addition["scenarios"]
    assert variant["name"] == "late-return~refine1"
    assert variant["vary"]["tier"] == {"values": ["standard"]}
    assert workload == before  # the original template is untouched
    runs = expand_scenarios({"scenarios": [variant]})
    assert len(runs) == 3 and all(run.name == "late-return~refine1" for run in runs)
    assert scenario_family(variant["name"]) == "late-return"


# --- few values --------------------------------------------------------------


def test_few_values_request_uses_a_value_a_claim_names() -> None:
    traces = [make("POST", "/invoice", {"customer_id": "1001", "sku": "WIDGET", "coupon": c}, status=200)
              for c in ("SAVE10", "VIP25")]
    claims = [{"id": "C5", "text": "x", "request": {"method": "GET", "path": "/quote",
                                                    "query": {"coupon": "SUMMER21"}}}]
    gap = gap_of("few_values", detect_evidence_gaps(traces, [], claims))
    addition, text = addition_for_gap(gap, ctx_for(traces, claims=claims))
    [request] = expand_workload(addition)
    assert request.method == "POST" and request.path == "/invoice"
    assert request.params == {"customer_id": "1001", "sku": "WIDGET", "coupon": "SUMMER21"}
    assert "SUMMER21" in text


def test_few_values_request_falls_back_to_an_obviously_invalid_value() -> None:
    traces = [make("POST", "/invoice", {"customer_id": "1001", "coupon": c}) for c in ("SAVE10", "VIP25")]
    gap = gap_of("few_values", detect_evidence_gaps(traces, [], []))
    addition, _ = addition_for_gap(gap, ctx_for(traces))
    [request] = expand_workload(addition)
    assert request.params["coupon"] == "refine-invalid"


def test_few_values_scenario_runs_one_run_with_a_third_value() -> None:
    template = {"name": "late-return", "vary": {"returned_on": {"values": ["2026-01-21", "2026-03-15"]},
                                                "tier": {"values": ["standard", None]}},
                "steps": [{"method": "POST", "path": "/loans"}]}
    traces = [make("POST", "/loans", {}, scenario={"name": "late-return", "run": i, "step": 0,
                                                   "vars": {"returned_on": v, "tier": "standard"}})
              for i, v in enumerate(("2026-01-21", "2026-03-15"))]
    claims = [{"id": "L7", "text": "x", "request": {"scenario": "late-return", "step": 2,
                                                    "vars": {"returned_on": "2026-02-02"}}}]
    gap = gap_of("few_values", detect_evidence_gaps(traces, [], claims))
    addition, _ = addition_for_gap(gap, ctx_for(traces, workload={"scenarios": [template]}, claims=claims))
    [run] = expand_scenarios({"scenarios": addition["scenarios"]})
    assert run.bindings == {"returned_on": "2026-02-02", "tier": "standard"}


# --- single statuses ---------------------------------------------------------


def test_unknown_ids_keep_the_observed_prefix_length_and_charset() -> None:
    ids = unknown_ids(["L-2baa5f", "L-d1bfe2", "L-0a1b2c"], observed={"L-2baa5f"}, count=3)
    assert len(ids) == 3 and "L-2baa5f" not in ids
    for value in ids:
        assert value.startswith("L-") and len(value) == len("L-2baa5f")
        assert all(c in "0123456789abcdef" for c in value[2:])
    assert unknown_ids(["1001", "1002"], observed=set())[0].isdigit()


def test_single_status_adds_unknown_ids_in_the_observed_format() -> None:
    traces = [make("GET", "/customer/1001/tier", body={"tier": "GOLD"}),
              make("GET", "/customer/5000/tier", body={"tier": "BRONZE"})]
    gap = gap_of("single_status", detect_evidence_gaps(traces, [], []))
    addition, _ = addition_for_gap(gap, ctx_for(traces))
    paths = [r.path for r in expand_workload(addition)]
    assert len(paths) == 2
    for path in paths:
        segment = path.split("/")[2]
        assert path.startswith("/customer/") and path.endswith("/tier")
        assert len(segment) == 4 and segment.isdigit() and segment not in {"1001", "5000"}


def test_single_status_tries_invalid_values_seen_for_the_same_parameter_name() -> None:
    traces = [make("GET", "/customer/1001/tier"), make("GET", "/customer/5000/tier"),
              make("GET", "/customer/abc/tier", status=404, body="not found"),
              make("POST", "/invoice", {"customer_id": "2000", "sku": "WIDGET"}, status=404)]
    gap = gap_of("single_status", detect_evidence_gaps(traces, [], []))
    addition, text = addition_for_gap(gap, ctx_for(traces))
    assert "/customer/abc/tier" in [r.path for r in expand_workload(addition)]
    assert "invalid customer_id abc" in text


def test_single_status_leaves_out_each_required_parameter() -> None:
    traces = [make("GET", "/quote", {"sku": "WIDGET", "qty": str(q)}) for q in (1, 2)]
    gap = gap_of("single_status", detect_evidence_gaps(traces, [], []))
    spec = {"endpoints": [{"endpoint": "GET /quote", "params": {
        "sku": {"required": True}, "qty": {"required": True}}}]}
    addition, _ = addition_for_gap(gap, ctx_for(traces, spec=spec))
    queries = [r.query_string for r in expand_workload(addition)]
    assert "qty=1" in queries and "sku=WIDGET" in queries


def test_placeholders_are_never_tried_as_invalid_ids() -> None:
    traces = [make("GET", "/loans/L-000001", body={"id": "L-000001"}),
              make("GET", "/loans/L-000002", body={"id": "L-000002"}),
              make("GET", "/loans/{{loan}}", status=404, body="not found")]
    gap = gap_of("single_status", detect_evidence_gaps(traces, [], []))
    addition, _ = addition_for_gap(gap, ctx_for(traces))
    assert not any("{{" in r.path for r in expand_workload(addition))


# --- gaps that are not acted on ----------------------------------------------


def test_failed_capture_and_unobserved_claim_are_not_acted_on_and_say_why() -> None:
    gap = {"kind": "failed_capture", "where": "w", "detail": "d", "suggestion": "s", "data": {}}
    addition, reason = addition_for_gap(gap, ctx_for())
    assert addition is None and "workload" in reason
    claim_gap = {"kind": "unobserved_claim", "where": "claim C1", "detail": "d", "suggestion": "s",
                 "data": {"claim": "C1", "request": {"method": "GET", "path": "/x"}}}
    addition, reason = addition_for_gap(claim_gap, ctx_for())
    assert addition is None and "targeted probing" in reason


# --- the loop, on a tiny system with a planted boundary and a single-status endpoint ----------

ITEMS = {"i-000001": "anvil", "i-000002": "bolt"}


def shop(environ, start_response):
    """A tiny WSGI shop: a discount from qty 45, and items that exist only for their own ids."""
    path = environ["PATH_INFO"]
    query = parse_qs(environ["QUERY_STRING"])
    if path == "/price":
        if "qty" not in query:
            status, body = "400 Bad Request", {"error": "MISSING_QTY"}
        else:
            qty = int(query["qty"][0])
            status, body = "200 OK", {"qty": qty, "discount": 5 if qty >= 45 else 0}
    elif path.startswith("/items/") and path.count("/") == 2 and path.rsplit("/", 1)[1] in ITEMS:
        item = path.rsplit("/", 1)[1]
        status, body = "200 OK", {"id": item, "name": ITEMS[item]}
    else:
        status, body = "404 Not Found", {"error": "NOT_FOUND"}
    data = json.dumps(body).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(data)))])
    return [data]


SHOP_WORKLOAD = {
    "requests": [{"method": "GET", "path": "/items/i-000001"}, {"method": "GET", "path": "/items/i-000002"}],
    "sweeps": [{"method": "GET", "path": "/price", "query": {}, "vary": {"qty": {"range": [0, 100, 10]}}}],
}


def shop_target(request: Request) -> Response:
    return call_wsgi(shop, request)


@pytest.fixture(scope="module")
def shop_refined():
    sink = ListSink()
    exercise_target(shop_target, SHOP_WORKLOAD, sink)
    traces = list(sink)
    before = copy.deepcopy(SHOP_WORKLOAD)
    result = refine(shop_target, SHOP_WORKLOAD, traces, rounds=3)
    return traces, before, result


def test_the_planted_inexact_boundary_is_found_and_closed(shop_refined) -> None:
    traces, _, result = shop_refined
    initial = [g for g in result.initial_gaps if g["kind"] == "inexact_change"]
    assert [(g["data"]["lo"], g["data"]["hi"]) for g in initial] == [("40", "50")]
    outcome = next(o for o in result.outcomes if o.gap["kind"] == "inexact_change")
    assert outcome.status == "closed" and outcome.requests == 9
    assert not [g for g in result.final_gaps if g["kind"] == "inexact_change"]
    exact = [t for t in result.traces if t.request.path == "/price" and t.request.params.get("qty") in ("44", "45")]
    assert {t.request.params["qty"]: json.loads(t.response.body)["discount"] for t in exact} == {"44": 0, "45": 5}


def test_the_single_status_endpoint_with_a_404_for_unknown_ids_is_closed(shop_refined) -> None:
    _, _, result = shop_refined
    items = next(o for o in result.outcomes if o.gap["where"] == "GET /items/{id}")
    assert items.status == "closed" and items.drew == "404 x2"
    assert not [g for g in result.final_gaps if g["where"] == "GET /items/{id}"]


def test_the_missing_parameter_of_a_single_status_endpoint_is_closed(shop_refined) -> None:
    _, _, result = shop_refined
    price = next(o for o in result.outcomes if o.gap["where"] == "GET /price")
    assert price.status == "closed" and "400" in price.drew


def test_refine_never_modifies_the_workload_and_appends_only_additions(shop_refined) -> None:
    _, before, result = shop_refined
    assert SHOP_WORKLOAD == before
    assert result.workload["requests"][:2] == before["requests"]
    assert result.workload["sweeps"][:1] == before["sweeps"]
    assert len(result.workload["sweeps"]) > len(before["sweeps"])
    assert len(result.workload["requests"]) > len(before["requests"])


def test_refined_traces_are_the_original_plus_the_new_exchanges(shop_refined) -> None:
    traces, _, result = shop_refined
    assert result.traces[: len(traces)] == traces
    assert result.added == len(result.traces) - len(traces) > 0


def test_the_refined_workload_expands_to_the_original_plus_the_additions(shop_refined) -> None:
    traces, _, result = shop_refined
    assert len(list(expand_workload(result.workload))) == len(list(expand_workload(SHOP_WORKLOAD))) + sum(
        1 for t in result.traces[len(traces):] if t.scenario is None)


def test_refine_log_reports_each_gap_with_its_addition_and_outcome(shop_refined) -> None:
    _, _, result = shop_refined
    log = render_refine_log(result)
    assert "## Round 1" in log and "closed" in log
    assert "| inexact_change | GET /price" in log
    assert "## Gaps left after refinement" in log


def test_gap_keys_are_stable_for_the_same_gap() -> None:
    gap = {"kind": "single_status", "where": "GET /x", "detail": "d", "suggestion": "s", "data": {}}
    assert gap_key(gap) == gap_key(dict(gap, detail="other"))


def test_refine_needs_the_full_recording_of_its_workload() -> None:
    with pytest.raises(ValueError, match="full recording"):
        refine(shop_target, SHOP_WORKLOAD, [], rounds=1)


def test_a_round_limit_of_zero_leaves_every_gap_not_attempted() -> None:
    sink = ListSink()
    exercise_target(shop_target, SHOP_WORKLOAD, sink)
    result = refine(shop_target, SHOP_WORKLOAD, list(sink), rounds=0)
    assert result.added == 0 and result.rounds == 0
    assert {o.status for o in result.outcomes} == {"not attempted"}


def test_empty_addition_sends_nothing() -> None:
    assert expand_workload(empty_addition()) == []


# --- the library: a stateful Node.js system, through its process ---------------------

library_only = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@library_only
def test_library_refinement_closes_what_it_can_and_explains_the_rest() -> None:
    workload = load_workload(ROOT / "legacy_library" / "workload.json")
    sink = ListSink()
    with ProcessTarget(f"node {ROOT / 'legacy_library' / 'server.js'}") as target:
        exercise_target(target, workload, sink)
        traces = list(sink)
        before = copy.deepcopy(workload)
        result = refine(target, workload, traces, [], rounds=1)
    assert workload == before
    statuses = {o.status for o in result.outcomes}
    assert statuses <= {"closed", "explained", "not acted on", "not attempted"}
    assert any(o.status == "closed" for o in result.outcomes)
    assert all(o.status == "not acted on" for o in result.outcomes if o.gap["kind"] == "failed_capture")
