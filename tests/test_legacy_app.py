"""Tests for the ACME Billing legacy sample app.

These pin the real (observed) behaviour, quirks included. The docs in
legacy_app/LEGACY_API.md are wrong in places on purpose; see claims.json.
"""

import json
import os
import re
from datetime import datetime
from urllib.parse import urlencode

from archaeology.trace import Request
from archaeology.wsgi import call_wsgi
from legacy_app.billing import make_app

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}
CLAIMS_PATH = os.path.join(os.path.dirname(__file__), "..", "legacy_app", "claims.json")
MONEY = re.compile(r"^\d+\.\d{2}$")


def _clean(d):
    return {k: v for k, v in (d or {}).items() if v is not None}


def send(app, method, path, query=None, form=None):
    qs = urlencode(_clean(query))
    if form is None:
        return call_wsgi(app, Request.build(method, path, query_string=qs))
    return call_wsgi(
        app,
        Request.build(method, path, query_string=qs, body=urlencode(_clean(form)), headers=FORM_HEADERS),
    )


def quote(app, **params):
    return send(app, "GET", "/quote", query=params)


def invoice(app, **fields):
    return send(app, "POST", "/invoice", form=fields)


def body_json(resp):
    return json.loads(resp.body)


# --- /quote basics --------------------------------------------------------

def test_quote_success_shape():
    r = quote(make_app(), sku="WIDGET", qty="3")
    assert r.status == 200
    assert r.headers["content-type"] == "application/json"
    assert body_json(r) == {
        "bulk_discount_pct": 0,
        "coupon": None,
        "qty": 3,
        "sku": "WIDGET",
        "subtotal": "14.97",
        "total": "14.97",
        "unit_price": "4.99",
    }


def test_json_is_sorted_keys():
    r = quote(make_app(), sku="GADGET")
    assert r.body == json.dumps(json.loads(r.body), sort_keys=True)


def test_missing_sku_is_text_400():
    r = quote(make_app(), qty="2")
    assert r.status == 400
    assert r.headers["content-type"] == "text/plain"
    assert r.body == "missing sku"


def test_empty_sku_counts_as_present_and_is_unknown():
    r = quote(make_app(), sku="")
    assert r.status == 200
    assert body_json(r) == {"error": "UNKNOWN_SKU", "sku": ""}


def test_unknown_sku_is_200_json_not_404():
    r = quote(make_app(), sku="NOPE")
    assert r.status == 200
    assert r.headers["content-type"] == "application/json"
    assert body_json(r) == {"error": "UNKNOWN_SKU", "sku": "NOPE"}


def test_sku_is_case_and_whitespace_insensitive():
    app = make_app()
    for raw in ("widget", " Widget ", "WIDGET  ", "\tWidget\n"):
        r = quote(app, sku=raw, qty="1")
        assert body_json(r)["sku"] == "WIDGET", raw
        assert body_json(r)["total"] == "4.99", raw


def test_qty_defaults_to_one():
    r = quote(make_app(), sku="WIDGET")
    assert body_json(r)["qty"] == 1
    assert body_json(r)["subtotal"] == "4.99"


def test_qty_not_an_integer_is_text_400():
    app = make_app()
    for raw in ("abc", "2.5", ""):
        r = quote(app, sku="WIDGET", qty=raw)
        assert r.status == 400, raw
        assert r.headers["content-type"] == "text/plain", raw
        assert r.body == "bad qty", raw


def test_qty_whitespace_is_stripped_before_int():
    r = quote(make_app(), sku="WIDGET", qty=" 4 ")
    assert body_json(r)["qty"] == 4


def test_qty_zero_and_negative_are_json_200_errors():
    app = make_app()
    r = quote(app, sku="WIDGET", qty="0")
    assert r.status == 200
    assert body_json(r) == {"error": "QTY_MUST_BE_POSITIVE", "qty": 0}
    r = quote(app, sku="WIDGET", qty="-5")
    assert body_json(r) == {"error": "QTY_MUST_BE_POSITIVE", "qty": -5}


def test_qty_above_1000_is_clamped_not_rejected():
    r = quote(make_app(), sku="WIDGET", qty="1500")
    assert r.status == 200
    data = body_json(r)
    assert data["qty"] == 1000
    assert data["subtotal"] == "4990.00"
    assert data["bulk_discount_pct"] == 15
    assert data["total"] == "4241.50"  # 4990.00 * 0.85


def test_qty_1000_is_not_clamped_further():
    assert body_json(quote(make_app(), sku="WIDGET", qty="1000"))["qty"] == 1000


def test_repeated_query_key_last_value_wins():
    app = make_app()
    r = call_wsgi(app, Request.build("GET", "/quote", query_string="sku=WIDGET&qty=1&qty=2"))
    assert body_json(r)["qty"] == 2


def test_empty_coupon_is_ignored():
    app = make_app()
    r = quote(app, sku="WIDGET", qty="1", coupon="")
    assert body_json(r)["coupon"] is None
    assert body_json(r)["total"] == "4.99"


def test_truncation_not_rounding():
    # 0.35 * 0.90 = 0.315 -> 0.31 (rounding would give 0.32)
    r = quote(make_app(), sku="DOOHICKEY", qty="1", coupon="SAVE10")
    assert body_json(r)["total"] == "0.31"
    assert body_json(r)["subtotal"] == "0.35"


def test_vip25_coupon_truncates():
    # 4.99 * 0.75 = 3.7425 -> 3.74
    r = quote(make_app(), sku="WIDGET", qty="1", coupon="VIP25")
    assert body_json(r)["coupon"] == "VIP25"
    assert body_json(r)["total"] == "3.74"


def test_coupon_is_case_and_whitespace_insensitive():
    app = make_app()
    for raw in ("save10", " SAVE10", "Save10 "):
        r = quote(app, sku="GIZMO", qty="1", coupon=raw)
        assert body_json(r)["coupon"] == "SAVE10", raw
        assert body_json(r)["total"] == "134.10", raw  # 149.00 * 0.90


def test_expired_and_unknown_coupons_are_silently_ignored():
    app = make_app()
    base = body_json(quote(app, sku="GIZMO", qty="1"))["total"]
    for code in ("SUMMER21", "BOGUS"):
        r = quote(app, sku="GIZMO", qty="1", coupon=code)
        assert r.status == 200, code
        assert body_json(r)["coupon"] is None, code
        assert body_json(r)["total"] == base, code
        assert "error" not in body_json(r), code


# --- bulk discount boundaries --------------------------------------------

def test_bulk_no_discount_at_100():
    data = body_json(quote(make_app(), sku="WIDGET", qty="100"))
    assert data["bulk_discount_pct"] == 0
    assert data["total"] == "499.00"


def test_bulk_10_percent_from_101():
    data = body_json(quote(make_app(), sku="WIDGET", qty="101"))
    assert data["bulk_discount_pct"] == 10
    assert data["total"] == "453.59"  # 503.99 * 0.90 = 453.591 truncated


def test_bulk_still_10_percent_at_499():
    data = body_json(quote(make_app(), sku="WIDGET", qty="499"))
    assert data["bulk_discount_pct"] == 10
    assert data["total"] == "2241.00"  # 2490.01 * 0.90 = 2241.009


def test_bulk_15_percent_at_500():
    data = body_json(quote(make_app(), sku="WIDGET", qty="500"))
    assert data["bulk_discount_pct"] == 15
    assert data["total"] == "2120.75"  # 2495.00 * 0.85


def test_coupon_and_bulk_both_apply():
    # 149.00 * 101 = 15049.00; * 0.75 = 11286.75; * 0.90 = 10158.075 -> 10158.07
    data = body_json(quote(make_app(), sku="GIZMO", qty="101", coupon="VIP25"))
    assert data["coupon"] == "VIP25"
    assert data["bulk_discount_pct"] == 10
    assert data["total"] == "10158.07"


# --- /invoice -------------------------------------------------------------

def test_invoice_success_gold():
    r = invoice(make_app(), customer_id="1001", sku="GIZMO", qty="1")
    assert r.status == 200
    assert r.headers["content-type"] == "application/json"
    data = body_json(r)
    assert data["invoice_id"] == "INV-000001"
    assert data["customer_id"] == "1001"
    assert data["tier"] == "GOLD"
    assert data["tier_discount_pct"] == 5
    assert data["line"] == {"sku": "GIZMO", "qty": 1, "coupon": None, "bulk_discount_pct": 0}
    assert data["total"] == "141.55"  # 149.00 * 0.95
    assert set(data) == {"invoice_id", "created_at", "customer_id", "tier", "tier_discount_pct", "line", "total"}
    datetime.fromisoformat(data["created_at"])


def test_invoice_ids_are_sequential():
    app = make_app()
    first = body_json(invoice(app, customer_id="1001", sku="WIDGET", qty="1"))
    second = body_json(invoice(app, customer_id="1001", sku="WIDGET", qty="1"))
    assert first["invoice_id"] == "INV-000001"
    assert second["invoice_id"] == "INV-000002"


def test_each_app_has_its_own_counter():
    a = make_app()
    b = make_app()
    invoice(a, customer_id="1001", sku="WIDGET")
    assert body_json(invoice(a, customer_id="1001", sku="WIDGET"))["invoice_id"] == "INV-000002"
    assert body_json(invoice(b, customer_id="1001", sku="WIDGET"))["invoice_id"] == "INV-000001"


def test_identical_invoices_have_different_volatile_ids():
    app = make_app()
    ids = {body_json(invoice(app, customer_id="1002", sku="WIDGET", qty="3", coupon="VIP25"))["invoice_id"]
           for _ in range(3)}
    assert len(ids) == 3


def test_failed_invoices_do_not_consume_a_number():
    app = make_app()
    assert invoice(app, sku="WIDGET").body == "missing customer_id"
    assert invoice(app, customer_id="1001", sku="WIDGET", qty="abc").status == 400
    assert invoice(app, customer_id="1001", sku="WIDGET", qty="0").status == 200
    assert body_json(invoice(app, customer_id="1001", sku="NOPE"))["error"] == "UNKNOWN_SKU"
    assert body_json(invoice(app, customer_id="1001", sku="WIDGET", qty="0"))["error"] == "QTY_MUST_BE_POSITIVE"
    assert body_json(invoice(app, customer_id="1001", sku="WIDGET"))["invoice_id"] == "INV-000001"


def test_invoice_missing_customer_id_is_text_400():
    r = invoice(make_app(), sku="WIDGET", qty="1")
    assert r.status == 400
    assert r.headers["content-type"] == "text/plain"
    assert r.body == "missing customer_id"


def test_invoice_pricing_errors_pass_through():
    app = make_app()
    r = invoice(app, customer_id="1001", sku="WIDGET")
    assert body_json(r)["invoice_id"] == "INV-000001"
    r = invoice(app, customer_id="1001", sku="WIDGET", qty="x")
    assert r.status == 400 and r.body == "bad qty"
    r = invoice(app, customer_id="1001")
    assert r.status == 400 and r.body == "missing sku"


def test_invoice_non_post_is_405():
    app = make_app()
    for method in ("GET", "PUT", "DELETE"):
        r = send(app, method, "/invoice")
        assert r.status == 405, method
        assert r.headers["content-type"] == "text/plain"
        assert r.body == "method not allowed", method


def test_invoice_unknown_customer_is_bronze_no_discount():
    data = body_json(invoice(make_app(), customer_id="9999", sku="WIDGET", qty="1"))
    assert data["tier"] == "BRONZE"
    assert data["tier_discount_pct"] == 0
    assert data["total"] == "4.99"


def test_invoice_silver_and_gold_discounts():
    app = make_app()
    silver = body_json(invoice(app, customer_id="1002", sku="GIZMO", qty="1"))
    assert silver["tier"] == "SILVER"
    assert silver["total"] == "146.02"  # 149.00 * 0.98
    gold = body_json(invoice(app, customer_id="1003", sku="GIZMO", qty="1"))
    assert gold["tier"] == "GOLD"
    assert gold["total"] == "141.55"


def test_invoice_customer_id_is_stripped():
    data = body_json(invoice(make_app(), customer_id=" 1001 ", sku="GIZMO", qty="1"))
    assert data["customer_id"] == "1001"
    assert data["tier"] == "GOLD"


def test_invoice_empty_customer_id_counts_as_present():
    data = body_json(invoice(make_app(), customer_id="", sku="WIDGET", qty="1"))
    assert data["tier"] == "BRONZE"


def test_invoice_total_discounts_applied_after_truncation():
    # pricing 0.31 (truncated), then GOLD 5%: 0.31 * 0.95 = 0.2945 -> 0.29
    data = body_json(invoice(make_app(), customer_id="1001", sku="DOOHICKEY", qty="1", coupon="SAVE10"))
    assert data["total"] == "0.29"


def test_invoice_bulk_then_tier_truncates():
    # 748.50 * 0.90 = 673.65 (pricing), then * 0.95 = 639.9675 -> 639.96
    data = body_json(invoice(make_app(), customer_id="1001", sku="WIDGET", qty="150"))
    assert data["line"]["bulk_discount_pct"] == 10
    assert data["total"] == "639.96"


def test_invoice_coupon_echo_and_normalisation():
    data = body_json(invoice(make_app(), customer_id="1001", sku=" gizmo ", qty="1", coupon="vip25"))
    assert data["line"]["sku"] == "GIZMO"
    assert data["line"]["coupon"] == "VIP25"


def test_invoice_expired_coupon_is_ignored():
    data = body_json(invoice(make_app(), customer_id="1004", sku="GIZMO", qty="1", coupon="SUMMER21"))
    assert data["line"]["coupon"] is None
    assert data["total"] == "146.02"  # SILVER only


# --- /customer/<id>/tier --------------------------------------------------

def test_tier_endpoint_known_customers():
    app = make_app()
    expected = {"1001": "GOLD", "1002": "SILVER", "1003": "GOLD", "1004": "SILVER"}
    for cid, tier in expected.items():
        r = send(app, "GET", "/customer/%s/tier" % cid)
        assert r.status == 200
        assert r.headers["content-type"] == "application/json"
        assert body_json(r) == {"customer_id": cid, "tier": tier}


def test_tier_endpoint_unknown_numeric_id_is_bronze_200():
    r = send(make_app(), "GET", "/customer/9999/tier")
    assert r.status == 200
    assert body_json(r) == {"customer_id": "9999", "tier": "BRONZE"}


def test_tier_endpoint_non_numeric_is_text_404():
    for cid in ("abc", "10a", "-1"):
        r = send(make_app(), "GET", "/customer/%s/tier" % cid)
        assert r.status == 404, cid
        assert r.headers["content-type"] == "text/plain", cid
        assert r.body == "not found", cid


def test_tier_endpoint_empty_or_extra_segments_are_404():
    app = make_app()
    for path in ("/customer//tier", "/customer/1001/tier/", "/customer/tier", "/customer/1001"):
        assert send(app, "GET", path).status == 404, path


def test_unknown_routes_are_text_404():
    app = make_app()
    for path in ("/nope", "/", "/quote/extra"):
        r = send(app, "GET", path)
        assert r.status == 404, path
        assert r.headers["content-type"] == "text/plain", path
        assert r.body == "not found", path


def test_quote_is_get_only():
    r = send(make_app(), "POST", "/quote", form={"sku": "WIDGET"})
    assert r.status == 404


def test_money_strings_have_two_decimals():
    app = make_app()
    samples = [
        quote(app, sku="DOOHICKEY", qty="1"),
        quote(app, sku="GIZMO", qty="7", coupon="VIP25"),
        quote(app, sku="WIDGET", qty="1000"),
        invoice(app, customer_id="1002", sku="GADGET", qty="2"),
    ]
    for r in samples:
        data = body_json(r)
        for key in ("subtotal", "total", "unit_price"):
            if key in data:
                assert MONEY.match(data[key]), (key, data[key])


# --- claims.json ----------------------------------------------------------

def _claim_request(spec):
    query = spec.get("query") or {}
    if "form" in spec:
        return Request.build(
            spec["method"], spec["path"],
            query_string=urlencode(query),
            body=urlencode(spec["form"]),
            headers=FORM_HEADERS,
        )
    return Request.build(spec["method"], spec["path"], query_string=urlencode(query))


def _evaluate(resp, expect):
    if "status" in expect and resp.status != expect["status"]:
        return False
    if "json_subset" in expect:
        data = resp.json()
        if not isinstance(data, dict):
            return False
        for key, value in expect["json_subset"].items():
            if key not in data or data[key] != value:
                return False
    if "body_equals" in expect and resp.body != expect["body_equals"]:
        return False
    return True


def test_claims_truth_table():
    with open(CLAIMS_PATH, encoding="utf-8") as fh:
        claims = json.load(fh)
    assert [c["id"] for c in claims] == ["C%d" % i for i in range(1, 10)]

    results = {}
    for claim in claims:
        app = make_app()
        resp = call_wsgi(app, _claim_request(claim["request"]))
        results[claim["id"]] = _evaluate(resp, claim["expect"])

    assert results == {
        "C1": False,
        "C2": False,
        "C3": False,
        "C4": False,
        "C5": False,
        "C6": False,
        "C7": True,
        "C8": False,
        "C9": True,
    }
