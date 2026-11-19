"""Tests for the inference engine, built from synthetic traces."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from archaeology.infer import (
    build_catalog,
    check_claims,
    detect_boundaries,
    detect_normalization,
    detect_flows,
    detect_volatile_fields,
    infer_spec,
    load_spec,
    path_template,
    render_report,
    save_spec,
    value_type,
    json_type,
    volatile_paths_for,
)
from archaeology.infer.catalog import endpoint_key
from archaeology.infer.volatility import IDENTIFIER_REASON, volatile_paths
from archaeology.trace import Request, Response, Trace

_ids = itertools.count(1)


def make(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    status: int = 200,
    body: Any = None,
    ctype: str = "application/json",
) -> Trace:
    """Build one trace; dict/list bodies are JSON-encoded, strings are sent as-is."""
    text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
    request = Request.build(method, path, query_string=urlencode(params or {}))
    response = Response(status=status, headers={"content-type": ctype}, body=text)
    return Trace(id=f"t-{next(_ids):06d}", ts="2026-11-18T00:00:00+00:00", request=request, response=response)


def quote_step_traces(sku: str = "WIDGET") -> list[Trace]:
    """A discount that is 0 up to qty 100 and 10 from qty 101, echoing qty."""
    return [
        make(
            "GET",
            "/quote",
            {"sku": sku, "qty": q},
            body={"sku": sku, "qty": q, "bulk_discount_pct": 0 if q <= 100 else 10},
        )
        for q in range(1, 201, 5)
    ]


# --- catalog -----------------------------------------------------------------


def test_path_template_and_endpoint_key() -> None:
    assert path_template("/customer/1001/tier") == "/customer/{id}/tier"
    assert path_template("/quote") == "/quote"
    assert path_template("/a/12b/3") == "/a/12b/{id}"
    assert endpoint_key(make("POST", "/customer/7/tier")) == "POST /customer/{id}/tier"


def test_path_template_hyphenated_catalogue_numbers() -> None:
    assert path_template("/books/978-0-00-000001-9") == "/books/{id}"
    assert path_template("/books/0-306-40615-X") == "/books/{id}"
    assert path_template("/v1-2/items") == "/v1-2/items"
    assert path_template("/sign-in") == "/sign-in"


def test_path_template_prefixed_hex_ids_without_digits() -> None:
    assert path_template("/loans/L-acdffe/return") == "/loans/{id}/return"
    assert path_template("/members/m_beadfeed") == "/members/{id}"
    assert path_template("/deadbeef") == "/deadbeef"


def test_value_and_json_types() -> None:
    assert [value_type(v) for v in ["", "42", "-3", "1.5", "1e3", "abc"]] == [
        "empty", "int", "int", "float", "float", "str",
    ]
    assert [json_type(v) for v in [True, 1, 1.0, None, {}, [], "x"]] == [
        "boolean", "integer", "number", "null", "object", "array", "string",
    ]


def test_catalog_templating_params_and_shapes() -> None:
    traces = [
        make("GET", "/customer/1001/tier", body={"id": "1001", "tier": "gold"}),
        make("GET", "/customer/1002/tier", body={"id": "1002", "tier": "silver"}),
        make("GET", "/quote", {"sku": "WIDGET", "qty": 3}, body={"sku": "WIDGET", "qty": 3, "total": "0.31"}),
        make("GET", "/quote", {"sku": "GADGET", "qty": 3}, body={"sku": "GADGET", "qty": 3, "total": "0.31"}),
        make("GET", "/quote", {"sku": "GADGET", "qty": "abc"}, status=400, body="bad qty", ctype="text/plain"),
        make("GET", "/quote", {"sku": "WIDGET"}, status=400, body="missing qty", ctype="text/plain"),
        make("GET", "/quote", {"sku": "GADGET", "qty": ""}, status=400, body="bad qty", ctype="text/plain"),
    ]
    catalog = build_catalog(traces)
    assert [e["endpoint"] for e in catalog] == ["GET /customer/{id}/tier", "GET /quote"]

    customer, quote = catalog
    assert customer["path"] == "/customer/{id}/tier" and customer["count"] == 2
    assert customer["params"] == {}
    assert customer["responses"] == [
        {
            "status": 200, "content_type": "application/json", "count": 2,
            "json_keys": ["id", "tier"],
            "field_types": {"id": ["string"], "tier": ["string"]},
            "text_examples": [],
        }
    ]

    assert quote["count"] == 5
    assert list(quote["params"]) == ["qty", "sku"]
    assert quote["params"]["qty"] == {
        "types": ["empty", "int", "str"], "required": False, "examples": ["3", "abc", ""],
    }
    assert quote["params"]["sku"] == {
        "types": ["str"], "required": True, "examples": ["WIDGET", "GADGET"],
    }
    ok, bad = quote["responses"]
    assert (ok["status"], ok["count"], ok["json_keys"]) == (200, 2, ["qty", "sku", "total"])
    assert ok["field_types"] == {"qty": ["integer"], "sku": ["string"], "total": ["string"]}
    assert (bad["status"], bad["count"], bad["json_keys"], bad["field_types"]) == (400, 3, None, None)
    assert bad["text_examples"] == ["bad qty", "missing qty"]


# --- volatility --------------------------------------------------------------


def test_volatility_identical_request_variance() -> None:
    traces = [
        make("GET", "/token", {"user": "ann"}, body={"token": "a1", "user": "ann"}),
        make("GET", "/token", {"user": "ann"}, body={"token": "zz9", "user": "ann"}),
        make("GET", "/token", {"user": "bob"}, body={"token": "q", "user": "bob"}),
    ]
    volatile = detect_volatile_fields(traces)
    assert volatile == {
        "GET /token": [{"path": "token", "reason": "varies across identical requests"}]
    }
    assert volatile_paths(volatile, "GET /token") == {"token"}


def test_volatility_timestamps() -> None:
    traces = [
        make("GET", "/orders", {"n": 1}, body={"created_at": "2026-11-18T10:00:00Z", "name": "x"}),
        make("GET", "/orders", {"n": 2}, body={"created_at": "2026-11-18 10:05:01.5", "name": "x"}),
        make("GET", "/orders", {"n": 3}, body={"created_at": "2026-11-18T10:07:00+00:00", "name": "x"}),
    ]
    assert detect_volatile_fields(traces) == {
        "GET /orders": [{"path": "created_at", "reason": "timestamp-like"}]
    }


def test_volatility_sequential_ids_and_non_monotonic_field() -> None:
    traces = [
        make("GET", "/invoice", {"n": 1}, body={"invoice_id": "INV-1001", "seq": 5, "ord": 7}),
        make("GET", "/invoice", {"n": 2}, body={"invoice_id": "INV-1002", "seq": 3, "ord": 8}),
        make("GET", "/invoice", {"n": 3}, body={"invoice_id": "INV-1003", "seq": 9, "ord": 9}),
    ]
    paths = {f["path"]: f["reason"] for f in detect_volatile_fields(traces)["GET /invoice"]}
    assert paths == {"invoice_id": "sequential identifier", "ord": "sequential identifier"}
    assert "seq" not in paths


def test_volatile_paths_for_uses_templated_endpoint() -> None:
    traces = [
        make("GET", "/token", {"user": "ann"}, body={"token": "a1"}),
        make("GET", "/token", {"user": "ann"}, body={"token": "b2"}),
    ]
    spec = infer_spec(traces)
    assert volatile_paths_for(spec, Request.build("GET", "/token", query_string="user=zed")) == {"token"}


# --- boundaries --------------------------------------------------------------


def test_step_function_changes_at_101() -> None:
    findings = detect_boundaries(quote_step_traces())
    steps = [f for f in findings if f["kind"] == "step"]
    assert len(steps) == 1
    step = steps[0]
    assert step["endpoint"] == "GET /quote"
    assert step["param"] == "qty" and step["field"] == "bulk_discount_pct"
    assert step["context"] == {"sku": "WIDGET"}
    assert step["changes_at"] == [101]
    assert [(s["value"], s["from"], s["to"]) for s in step["segments"]] == [(0, 1, 96), (10, 101, 196)]
    assert step["description"] == "GET /quote (sku=WIDGET): bulk_discount_pct is 0 for qty 1-96, 10 for qty 101-196"
    # The echoed qty follows the input everywhere, so it is not a clamp.
    assert not [f for f in findings if f["kind"] == "clamp"]


def test_clamp_at_1000() -> None:
    qtys = [1, 2, 5, 10, 50, 100, 101, 200, 500, 800, 999, 1000, 1001, 1200, 1500]
    traces = [
        make("GET", "/quote", {"sku": "WIDGET", "qty": q}, body={"sku": "WIDGET", "qty": min(q, 1000)})
        for q in qtys
    ]
    clamps = [f for f in detect_boundaries(traces) if f["kind"] == "clamp"]
    assert len(clamps) == 1
    clamp = clamps[0]
    assert (clamp["field"], clamp["echo_until"], clamp["then"]) == ("qty", 1000, 1000)
    assert clamp["description"] == "GET /quote (sku=WIDGET): qty follows the input up to 1000, then stays at 1000"


def test_shape_change_at_zero() -> None:
    traces = []
    for q in range(0, 201, 5):
        if q == 0:
            body: dict[str, Any] = {"error": "quantity must be positive", "qty": 0}
        else:
            body = {"bulk_discount_pct": 0 if q <= 100 else 10, "coupon": None, "qty": q, "sku": "WIDGET"}
        traces.append(make("GET", "/quote", {"sku": "WIDGET", "qty": q}, body=body))
    shapes = [f for f in detect_boundaries(traces) if f["kind"] == "shape"]
    assert len(shapes) == 1
    shape = shapes[0]
    assert shape["field"] == "shape"
    assert [(s["value"], s["from"], s["to"]) for s in shape["segments"]] == [
        ("200 error,qty", 0, 0),
        ("200 bulk_discount_pct,coupon,qty,sku", 5, 200),
    ]
    assert shape["description"] == (
        "GET /quote (sku=WIDGET): response shape is '200 error,qty' for qty 0-0, "
        "'200 bulk_discount_pct,coupon,qty,sku' for qty 5-200"
    )


def test_context_dedupe_counts_identical_findings() -> None:
    traces = quote_step_traces("WIDGET") + quote_step_traces("GADGET")
    steps = [f for f in detect_boundaries(traces) if f["field"] == "bulk_discount_pct"]
    assert len(steps) == 1
    assert steps[0]["contexts"] == 2
    assert steps[0]["changes_at"] == [101]


# --- normalization -----------------------------------------------------------


def test_case_insensitive_and_whitespace_trimmed() -> None:
    traces = [
        make("GET", "/quote", {"sku": sku, "qty": "3"}, body={"sku": sku.strip().upper(), "qty": 3})
        for sku in ["WIDGET", "widget", "  WIDGET", "WIDGET  "]
    ]
    findings = {f["behavior"]: f for f in detect_normalization(traces) if f["param"] == "sku"}
    assert set(findings) == {"case-insensitive", "whitespace-trimmed"}
    assert findings["case-insensitive"]["evidence_pairs"] == 1
    assert findings["whitespace-trimmed"]["evidence_pairs"] == 3
    assert findings["case-insensitive"]["description"] == (
        "GET /quote: sku is case-insensitive (1 request pairs differing only by case "
        "gave identical responses)"
    )
    assert not [f for f in detect_normalization(traces) if f["param"] == "qty"]


def test_case_sensitive_param() -> None:
    traces = [
        make(
            "GET", "/coupon", {"sku": "WIDGET", "coupon": c},
            body={"valid": True} if c == "SAVE10" else {"valid": False, "error": "INVALID_COUPON"},
        )
        for c in ["SAVE10", "save10", "SAVE10", "save10"]
    ]
    findings = detect_normalization(traces)
    assert len(findings) == 1
    assert findings[0]["param"] == "coupon"
    assert findings[0]["behavior"] == "case-sensitive"
    assert findings[0]["evidence_pairs"] == 4
    assert findings[0]["equivalent_pairs"] == 0


# --- claims ------------------------------------------------------------------


def claim_traces() -> tuple[list[Trace], list[dict[str, Any]]]:
    quote = make("GET", "/quote", {"sku": "WIDGET", "qty": 3}, body={"sku": "WIDGET", "total": "0.31"})
    coupon = make("GET", "/coupon", {"coupon": "bogus"}, body={"valid": False})
    claims = [
        {
            "id": "C-1", "text": "Quote total is 0.31",
            "request": {"method": "GET", "path": "/quote", "query": {"sku": "WIDGET", "qty": 3}},
            "expect": {"status": 200, "json_subset": {"total": "0.31"}},
        },
        {
            "id": "C-2", "text": "Quote total is 0.32 and returns 404",
            "request": {"method": "GET", "path": "/quote", "query": {"sku": "WIDGET", "qty": 3}},
            "expect": {"status": 404, "json_subset": {"total": "0.32"}},
        },
        {
            "id": "C-3", "text": "Bogus coupon returns INVALID_COUPON",
            "request": {"method": "GET", "path": "/coupon", "query": {"coupon": "bogus"}},
            "expect": {"json_subset": {"error": "INVALID_COUPON"}},
        },
        {
            "id": "C-4", "text": "Tier lookup for customer 42",
            "request": {"method": "GET", "path": "/customer/42/tier"},
            "expect": {"status": 200},
        },
    ]
    return [quote, coupon], claims


def test_claims_confirmed_contradicted_unobserved() -> None:
    traces, claims = claim_traces()
    results = {r["id"]: r for r in check_claims(claims, traces)}
    assert [r["id"] for r in check_claims(claims, traces)] == ["C-1", "C-2", "C-3", "C-4"]

    assert results["C-1"]["verdict"] == "CONFIRMED"
    assert results["C-1"]["trace_id"] == traces[0].id
    assert results["C-1"]["observed"]["status"] == 200
    assert results["C-1"]["mismatches"] == []

    assert results["C-2"]["verdict"] == "CONTRADICTED"
    assert results["C-2"]["mismatches"] == [
        "status: expected 404, observed 200",
        "total: expected '0.32', observed '0.31'",
    ]

    assert results["C-3"]["verdict"] == "CONTRADICTED"
    assert results["C-3"]["mismatches"] == ["error: expected 'INVALID_COUPON', observed <missing>"]

    assert results["C-4"]["verdict"] == "UNOBSERVED"
    assert results["C-4"]["trace_id"] is None and results["C-4"]["observed"] is None


# --- spec, report, persistence ----------------------------------------------


def test_render_report_contains_sections() -> None:
    traces, claims = claim_traces()
    traces = traces + quote_step_traces() + [
        make("GET", "/token", {"user": "ann"}, body={"token": "a1"}),
        make("GET", "/token", {"user": "ann"}, body={"token": "b2"}),
    ]
    report = render_report(infer_spec(traces, claims))
    for heading in [
        "# Behaviour report",
        "## Documentation vs reality",
        "## Hidden behaviour",
        "## Volatile fields",
        "## Endpoints",
    ]:
        assert heading in report
    assert "| id | claim | verdict | evidence |" in report
    assert report.index("| C-2 |") < report.index("| C-1 |")  # CONTRADICTED first
    assert "bulk_discount_pct is 0 for qty 1-96" in report
    assert "`GET /token` `token`: varies across identical requests" in report


def test_spec_round_trip(tmp_path: Path) -> None:
    traces, claims = claim_traces()
    spec = infer_spec(traces + quote_step_traces(), claims)
    assert spec["version"] == 2
    assert spec["trace_count"] == len(traces) + len(quote_step_traces())

    path = tmp_path / "spec.json"
    save_spec(spec, path)
    assert load_spec(path) == json.loads(json.dumps(spec))
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(spec))


def test_value_type_treats_padded_and_special_numbers_as_strings():
    from archaeology.infer.catalog import value_type

    assert value_type("2.5") == "float"
    assert value_type("1e3") == "float"
    assert value_type(" 1001") == "str"
    assert value_type("nan") == "str"
    assert value_type("inf") == "str"


# --- stateful systems (milestone 2) ----------------------------------------


def in_scenario(trace: Trace, name: str, run: int, step: int, variables: dict[str, str] | None = None) -> Trace:
    """Tag a trace as one step of a scenario run, as the recorder does."""
    trace.scenario = {"name": name, "run": run, "step": step, "vars": dict(variables or {})}
    return trace


def loan_run(run: int, returned_on: str, *, name: str = "late-return", status: int = 201) -> list[Trace]:
    """Members, borrow, return: the identifiers come from earlier responses, as in Pinecrest."""
    member = f"m_{0x3FA9C2D1 + run:x}"
    loan = f"L-{0x04B7E2 + run:06x}"
    variables = {"returned_on": returned_on}
    return [
        in_scenario(make("POST", "/members", {"name": "Ada"}, status=201, body={"member_id": member}),
                    name, run, 0, variables),
        in_scenario(make("POST", "/loans", {"member_id": member, "isbn": "9780000000011"},
                         status=201, body={"loan_id": loan, "member_id": member}),
                    name, run, 1, variables),
        in_scenario(make("POST", f"/loans/{loan}/return", {"returned_on": returned_on}, status=status,
                         body={"status": "returned"} if status == 201 else {"error": "LOAN_CLOSED"}),
                    name, run, 2, variables),
    ]


def days(start: str, stop: str) -> list[str]:
    from datetime import date, timedelta

    first, last = date.fromisoformat(start), date.fromisoformat(stop)
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


def test_path_template_templates_opaque_identifiers_only() -> None:
    assert path_template("/members/m_3fa9c2d1") == "/members/{id}"
    assert path_template("/loans/L-04b7e2/return") == "/loans/{id}/return"
    assert path_template("/x/9f86d081") == "/x/{id}"
    assert path_template("/u/123e4567-e89b-12d3-a456-426614174000") == "/u/{id}"
    assert path_template("/books/facade/members/return") == "/books/facade/members/return"
    assert path_template("/a/deadbeef") == "/a/deadbeef"


def test_value_type_recognises_calendar_dates() -> None:
    from archaeology.infer.catalog import value_type as vt

    assert [vt(v) for v in ["2026-01-05", "2026-02-30", "2026-1-5", "2026-01-05T10:00"]] == [
        "date", "str", "str", "str",
    ]
    traces = [make("GET", "/fee", {"returned_on": d}, body={"ok": True}) for d in ["2026-01-05", "2026-01-06"]]
    assert build_catalog(traces)[0]["params"]["returned_on"]["types"] == ["date"]


def fee_for(day: str) -> dict[str, Any]:
    """Grace period to the due date plus two days, then 0.25 a day, capped at 10.00."""
    from datetime import date
    from decimal import Decimal

    late = max(0, (date.fromisoformat(day) - date(2026, 1, 19)).days)
    fee = Decimal("0") if late <= 2 else min(Decimal("0.25") * late, Decimal("10.00"))
    return {"returned_on": day, "days_late": late, "fee": f"{fee:.2f}"}


def test_date_sweep_finds_grace_jump_rate_and_cap() -> None:
    traces = [make("GET", "/fee", {"returned_on": d}, body=fee_for(d)) for d in days("2026-01-10", "2026-03-15")]
    linear = {f["field"]: f for f in detect_boundaries(traces) if f["kind"] == "linear"}

    fee = linear["fee"]
    assert fee["segments"] == [
        {"from": "2026-01-10", "to": "2026-01-21", "start": "0.00", "end": "0.00", "slope": "0"},
        {"from": "2026-01-22", "to": "2026-02-28", "start": "0.75", "end": "10.00", "slope": "0.25"},
        {"from": "2026-03-01", "to": "2026-03-15", "start": "10.00", "end": "10.00", "slope": "0"},
    ]
    assert fee["changes_at"] == ["2026-01-22", "2026-03-01"]
    assert fee["description"] == (
        "GET /fee: fee is 0.00 from 2026-01-10 to 2026-01-21; 0.75 at 2026-01-22 rising "
        "0.25 per day to 10.00 at 2026-02-28; 10.00 from 2026-03-01 to 2026-03-15"
    )
    # days_late is 0 until the due date, then rises one a day.
    late = linear["days_late"]
    assert [(s["start"], s["slope"]) for s in late["segments"]] == [("0", "0"), ("1", "1")]
    assert late["changes_at"] == ["2026-01-20"]
    # The echoed date is a clamp question, never a linear one; nothing else is reported as linear.
    assert set(linear) == {"fee", "days_late"}


def test_int_sweep_linear_uses_per_param_unit() -> None:
    traces = [make("GET", "/ship", {"qty": q}, body={"fee": str(min(2 * (q - 10), 30)) if q > 10 else "0"})
              for q in range(1, 31)]
    linear = [f for f in detect_boundaries(traces) if f["kind"] == "linear"]
    assert len(linear) == 1
    assert linear[0]["description"] == (
        "GET /ship: fee is 0 from 1 to 10; 2 at 11 rising 2 per qty to 30 at 25; 30 from 26 to 30"
    )
    assert linear[0]["changes_at"] == [11, 26]


def test_scenario_step_shape_changes_at_a_date_and_stays_per_step() -> None:
    traces: list[Trace] = []
    for run, day in enumerate(days("2026-01-15", "2026-02-20")):
        status = 201 if day <= "2026-02-01" else 403
        traces += loan_run(run, day, status=status)
    findings = [f for f in detect_boundaries(traces) if f["kind"] == "shape" and f.get("scenario")]
    assert {f["step"] for f in findings} == {2}
    step = findings[0]
    assert step["scenario"] == "late-return"
    assert step["endpoint"] == "POST /loans/{id}/return"
    assert step["param"] == "returned_on"
    assert step["changes_at"] == ["2026-02-02"]
    assert step["description"].startswith(
        "late-return step 2 (POST /loans/{id}/return): response shape is '201 status' for returned_on 2026-01-15 to 2026-02-01"
    )


def test_identical_scenario_findings_on_two_steps_are_reported_once() -> None:
    traces: list[Trace] = []
    for run, day in enumerate(days("2026-01-15", "2026-02-20")):
        status = 201 if day <= "2026-02-01" else 403
        traces += loan_run(run, day, status=status)
        # A second, identical return at step 3 of the same scenario.
        extra = loan_run(run, day, status=status)[2]
        traces.append(in_scenario(extra, "late-return", run, 3, {"returned_on": day}))
    shapes = [f for f in detect_boundaries(traces) if f["kind"] == "shape" and f.get("scenario")]
    assert [(f["step"], f["steps"], f["changes_at"][0]) for f in shapes] == [(2, [2, 3], "2026-02-02")]
    assert shapes[0]["description"].startswith("late-return steps 2, 3 (POST /loans/{id}/return)")


def test_scenario_findings_that_differ_between_steps_stay_distinct() -> None:
    traces: list[Trace] = []
    for run, day in enumerate(days("2026-01-15", "2026-02-20")):
        traces += loan_run(run, day, status=201 if day <= "2026-02-01" else 403)
        # Step 3 changes on a different day from step 2.
        extra = loan_run(run, day, status=201 if day <= "2026-02-10" else 403)[2]
        traces.append(in_scenario(extra, "late-return", run, 3, {"returned_on": day}))
    shapes = sorted((f["step"], f["steps"], f["changes_at"][0]) for f in detect_boundaries(traces) if f["kind"] == "shape" and f.get("scenario"))
    assert shapes == [(2, [2], "2026-02-02"), (3, [3], "2026-02-11")]


def test_scenario_sweeps_do_not_leak_into_request_parameter_sweeps() -> None:
    traces = [t for day in days("2026-01-15", "2026-02-20") for t in loan_run(len(day), day)]
    assert all(f.get("scenario") for f in detect_boundaries(traces) if f["endpoint"].startswith("POST /loans/"))
    assert not [f for f in detect_boundaries(traces) if f["endpoint"] == "POST /members" and "scenario" not in f]


def test_identifier_flows_link_responses_to_later_requests_within_a_run() -> None:
    traces: list[Trace] = []
    for run in range(3):
        traces += loan_run(run, "2026-01-05")
    stray = make("GET", "/members/m_3fa9c2d1", body={"member_id": "m_3fa9c2d1"})  # stateless: no link
    flows = detect_flows(traces + [stray])
    assert [(f["from"], f["field"], f["to"], f["as"], f["count"]) for f in flows] == [
        ("POST /loans", "loan_id", "POST /loans/{id}/return", "path", 3),
        ("POST /members", "member_id", "POST /loans", "param member_id", 3),
    ]
    assert flows[1]["description"] == (
        "member_id from POST /members is sent to POST /loans as param member_id (3 times)"
    )


def test_identifier_flows_need_at_least_two_uses() -> None:
    assert detect_flows(loan_run(0, "2026-01-05")) == []


def test_generated_identifiers_are_volatile_and_constant_timestamps_are_not() -> None:
    traces: list[Trace] = []
    for run in range(3):
        traces += loan_run(run, "2026-01-05")
    traces += [make("GET", "/clock", {"n": n}, body={"at": "2026-11-18T10:00:00Z", "name": "x"}) for n in range(3)]
    volatile = detect_volatile_fields(traces)
    # POST /members already varies across identical requests, which keeps its reason.
    assert volatile["POST /members"] == [{"path": "member_id", "reason": "varies across identical requests"}]
    assert volatile["POST /loans"] == [{"path": "loan_id", "reason": IDENTIFIER_REASON}]
    assert "GET /clock" not in volatile


def test_separator_insensitive_normalization() -> None:
    traces = [
        make("POST", "/loans", {"member_id": "m1", "isbn": isbn}, body={"ok": True, "member": "m1"})
        for isbn in ["978-0-00-0000011", "9780000000011", "978 0 00 0000011"]
    ]
    (finding,) = [f for f in detect_normalization(traces) if f["param"] == "isbn"]
    assert finding["behavior"] == "separator-insensitive"
    assert finding["evidence_pairs"] == 3 and finding["equivalent_pairs"] == 3
    assert finding["description"] == (
        "POST /loans: isbn is separator-insensitive (3 request pairs differing only by hyphens or "
        "spaces gave identical responses)"
    )


def test_separator_sensitive_normalization() -> None:
    traces = [
        make("POST", "/loans", {"member_id": "m1", "isbn": isbn}, body={"isbn": isbn})
        for isbn in ["978-0-00-0000011", "9780000000011"]
    ]
    (finding,) = [f for f in detect_normalization(traces) if f["param"] == "isbn"]
    assert finding["behavior"] == "separator-sensitive"


def test_claims_about_scenario_steps() -> None:
    traces = loan_run(0, "2026-02-01", status=403) + loan_run(1, "2026-01-01", status=201)
    claims = [
        {"id": "S-1", "text": "Late return on 2026-02-01 is refused",
         "request": {"scenario": "late-return", "step": 2, "vars": {"returned_on": "2026-02-01"}},
         "expect": {"status": 403}},
        {"id": "S-2", "text": "Any return of step 2 is accepted",
         "request": {"scenario": "late-return", "step": 2},
         "expect": {"status": 201}},
        {"id": "S-3", "text": "No such run exists",
         "request": {"scenario": "late-return", "step": 2, "vars": {"returned_on": "2030-01-01"}},
         "expect": {"status": 201}},
    ]
    results = {r["id"]: r for r in check_claims(claims, traces)}
    assert results["S-1"]["verdict"] == "CONFIRMED"
    assert results["S-2"]["verdict"] == "CONTRADICTED" and results["S-2"]["trace_id"] == traces[2].id
    assert results["S-3"]["verdict"] == "UNOBSERVED"


def test_path_template_and_endpoint_key_for_scenario_steps() -> None:
    assert endpoint_key(loan_run(0, "2026-01-05")[2]) == "POST /loans/{id}/return"


def notes_run(run: int) -> list[Trace]:
    """Create, get, archive, get: the same GET path is recorded before and after a state change."""
    note = f"n-{0x1A2B3C + run:06x}"
    name = "notes"
    return [
        in_scenario(make("POST", "/notes", {"title": "todo"}, status=201, body={"id": note, "archived": False}), name, run, 0),
        in_scenario(make("GET", f"/notes/{note}", body={"id": note, "archived": False}), name, run, 1),
        in_scenario(make("POST", f"/notes/{note}/archive", status=200, body={"id": note, "archived": True}), name, run, 2),
        in_scenario(make("GET", f"/notes/{note}", body={"id": note, "archived": True}), name, run, 3),
    ]


def test_state_change_between_steps_is_not_volatility() -> None:
    traces: list[Trace] = []
    for run in range(3):
        traces += notes_run(run)
    volatile = detect_volatile_fields(traces)
    get_fields = {f["path"] for f in volatile.get("GET /notes/{id}", [])}
    assert "archived" not in get_fields
    assert "id" not in get_fields  # echoes the path; constant within a step
    # The generated id still is volatile: it differs across runs at the same step.
    assert {f["path"] for f in volatile["POST /notes"]} >= {"id"}


def test_stateless_and_scenario_traces_are_never_compared() -> None:
    one = make("GET", "/notes/n-1a2b3c", body={"archived": False})
    # A single stateless GET never pairs with the scenario GETs on the same path.
    mixed = detect_volatile_fields([one] + notes_run(0))
    assert "archived" not in {f["path"] for f in mixed.get("GET /notes/{id}", [])}
    # Stateless traces keep the existing rule: two identical requests that differ vary.
    two = [one, make("GET", "/notes/n-1a2b3c", body={"archived": True})]
    assert detect_volatile_fields(two)["GET /notes/{id}"] == [
        {"path": "archived", "reason": "varies across identical requests"}
    ]
