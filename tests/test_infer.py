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
from archaeology.infer.volatility import volatile_paths
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
    assert spec["version"] == 1
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
