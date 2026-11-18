"""Focused tests for the preserved ACME Billing behaviours.

Each test pins one behaviour observed in traces.jsonl, so a failure names the
quirk that changed.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from modern_app.main import (
    CATALOGUE,
    bulk_discount_pct,
    create_app,
    normalise_coupon,
    price_line,
    to_cents,
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def assert_text(response, status: int, body: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "text/plain"
    assert response.text == body


def post_invoice(client: TestClient, form: str):
    return client.post(
        "/invoice", content=form, headers={"content-type": "application/x-www-form-urlencoded"}
    )


# --- /quote ---------------------------------------------------------------- #

def test_quote_happy_path_body_and_types(client: TestClient) -> None:
    response = client.get("/quote", params={"sku": "WIDGET", "qty": "3"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "bulk_discount_pct": 0,
        "coupon": None,
        "qty": 3,
        "sku": "WIDGET",
        "subtotal": "14.97",
        "total": "14.97",
        "unit_price": "4.99",
    }


def test_unknown_sku_is_http_200_with_error_body(client: TestClient) -> None:
    response = client.get("/quote", params={"sku": "NOPE"})
    assert response.status_code == 200
    assert response.json() == {"error": "UNKNOWN_SKU", "sku": "NOPE"}


def test_sku_is_case_and_whitespace_insensitive(client: TestClient) -> None:
    response = client.get("/quote", params={"sku": "  widget ", "qty": "1"})
    assert response.json()["sku"] == "WIDGET"


def test_zero_and_negative_qty_are_http_200_errors(client: TestClient) -> None:
    assert client.get("/quote", params={"sku": "WIDGET", "qty": "0"}).json() == {
        "error": "QTY_MUST_BE_POSITIVE",
        "qty": 0,
    }
    assert client.get("/quote", params={"sku": "GIZMO", "qty": "-5"}).json() == {
        "error": "QTY_MUST_BE_POSITIVE",
        "qty": -5,
    }


def test_missing_qty_defaults_to_one(client: TestClient) -> None:
    assert client.get("/quote", params={"sku": "GIZMO"}).json()["qty"] == 1


@pytest.mark.parametrize("qty", ["abc", "", "2.5"])
def test_bad_qty_is_plain_text_400(client: TestClient, qty: str) -> None:
    assert_text(client.get("/quote", params={"sku": "GIZMO", "qty": qty}), 400, "bad qty")


def test_missing_sku_is_plain_text_400(client: TestClient) -> None:
    assert_text(client.get("/quote", params={"qty": "3"}), 400, "missing sku")


def test_quantity_is_clamped_to_1000(client: TestClient) -> None:
    body = client.get("/quote", params={"sku": "WIDGET", "qty": "1500"}).json()
    assert body["qty"] == 1000
    assert body["subtotal"] == "4990.00"
    assert body["total"] == "4241.50"


@pytest.mark.parametrize(
    ("qty", "pct"),
    [(100, 0), (101, 10), (499, 10), (500, 15)],
)
def test_bulk_discount_boundaries(qty: int, pct: int) -> None:
    assert bulk_discount_pct(qty) == pct


def test_totals_truncate_rather_than_round() -> None:
    # 0.35 * 0.9 = 0.315; the legacy system returns 0.31, not 0.32.
    assert price_line(Decimal("0.35"), 1, "SAVE10").total == Decimal("0.31")
    # 14.97 * 0.75 = 11.2275, returned as 11.22.
    assert price_line(CATALOGUE["WIDGET"], 3, "VIP25").total == Decimal("11.22")
    assert to_cents(Decimal("11.2275")) == Decimal("11.22")


def test_quote_coupon_and_bulk_combine_before_one_truncation(client: TestClient) -> None:
    # Trace: WIDGET x165 SAVE10 -> 666.91. Subtotal 823.35 * 0.9 * 0.9 = 666.9135.
    # Truncating after the bulk step (741.01 -> 666.90) would be wrong.
    body = client.get("/quote", params={"sku": "WIDGET", "qty": "165", "coupon": "SAVE10"}).json()
    assert body["bulk_discount_pct"] == 10
    assert body["subtotal"] == "823.35"
    assert body["total"] == "666.91"


def test_quote_coupon_and_bulk_at_upper_tier(client: TestClient) -> None:
    # Trace: WIDGET x600 VIP25 -> 1908.67. 2994.00 * 0.85 * 0.75 = 1908.675.
    body = client.get("/quote", params={"sku": "WIDGET", "qty": "600", "coupon": "VIP25"}).json()
    assert body["bulk_discount_pct"] == 15
    assert body["total"] == "1908.67"


def test_invoice_coupon_bulk_and_tier_use_the_truncated_quote_total(client: TestClient) -> None:
    # Trace: customer 1001 (GOLD), WIDGET x103, VIP25 -> 329.57.
    # Quote total 346.92 (513.97 * 0.9 * 0.75 = 346.929..., truncated), then 5% off.
    body = post_invoice(client, "customer_id=1001&sku=WIDGET&qty=103&coupon=VIP25").json()
    assert body["total"] == "329.57"
    assert body["tier_discount_pct"] == 5


def test_invoice_small_coupon_case_uses_tier_on_truncated_total(client: TestClient) -> None:
    # Trace: customer 1002 (SILVER), WIDGET x3, VIP25 -> 10.99 (11.22 then 2% off).
    body = post_invoice(client, "customer_id=1002&sku=WIDGET&qty=3&coupon=VIP25").json()
    assert body["total"] == "10.99"


# --- coupons ---------------------------------------------------------------- #

@pytest.mark.parametrize("raw", ["save10", " SAVE10", "SAVE10 "])
def test_coupon_is_case_and_whitespace_insensitive(raw: str) -> None:
    assert normalise_coupon(raw) == "SAVE10"


@pytest.mark.parametrize("raw", ["SUMMER21", "BOGUS", ""])
def test_unknown_or_blank_coupon_is_ignored(client: TestClient, raw: str) -> None:
    body = client.get("/quote", params={"sku": "WIDGET", "qty": "3", "coupon": raw}).json()
    assert body["coupon"] is None
    assert body["total"] == "14.97"


def test_plus_encoded_coupon_is_trimmed_in_the_query_string(client: TestClient) -> None:
    # In a query string '+' decodes to a space, so the code arrives as ' SAVE10'.
    body = client.get("/quote?sku=WIDGET&qty=3&coupon=+SAVE10").json()
    assert body["coupon"] == "SAVE10"
    assert body["total"] == "13.47"


def test_vip25_coupon_on_quote(client: TestClient) -> None:
    body = client.get("/quote", params={"sku": "WIDGET", "qty": "3", "coupon": "VIP25"}).json()
    assert body["coupon"] == "VIP25"
    assert body["total"] == "11.22"


# --- /invoice --------------------------------------------------------------- #

def test_invoice_applies_bulk_then_coupon_then_tier(client: TestClient) -> None:
    # 748.50 -> 673.65 (10% bulk) -> 505.23 (25% VIP25) -> 479.96 (5% GOLD).
    body = post_invoice(client, "customer_id=1001&sku=WIDGET&qty=150&coupon=VIP25").json()
    assert body["total"] == "479.96"
    assert body["tier"] == "GOLD"
    assert body["tier_discount_pct"] == 5
    assert body["line"] == {"bulk_discount_pct": 10, "coupon": "VIP25", "qty": 150, "sku": "WIDGET"}
    assert body["customer_id"] == "1001"


def test_invoice_customer_id_is_trimmed(client: TestClient) -> None:
    body = post_invoice(client, "customer_id=+1001&sku=WIDGET&qty=150").json()
    assert body["customer_id"] == "1001"


def test_invoice_numbers_advance_only_on_success() -> None:
    client = TestClient(create_app())
    first = post_invoice(client, "customer_id=1001&sku=GIZMO&qty=1").json()
    assert post_invoice(client, "customer_id=1003&sku=NOPE&qty=1").json() == {
        "error": "UNKNOWN_SKU",
        "sku": "NOPE",
    }
    second = post_invoice(client, "customer_id=1002&sku=WIDGET&qty=3").json()
    assert first["invoice_id"] == "INV-000001"
    assert second["invoice_id"] == "INV-000002"


def test_each_app_starts_a_fresh_invoice_counter() -> None:
    first = TestClient(create_app())
    assert post_invoice(first, "customer_id=1001&sku=GIZMO&qty=1").json()["invoice_id"] == "INV-000001"
    second = TestClient(create_app())
    assert post_invoice(second, "customer_id=1001&sku=GIZMO&qty=1").json()["invoice_id"] == "INV-000001"


def test_invoice_missing_customer_is_plain_text_400(client: TestClient) -> None:
    assert_text(post_invoice(client, "sku=WIDGET&qty=150"), 400, "missing customer_id")


def test_invoice_bad_qty_is_plain_text_400(client: TestClient) -> None:
    assert_text(post_invoice(client, "customer_id=1004&sku=GADGET&qty=abc"), 400, "bad qty")


def test_invoice_unknown_sku_is_http_200_error(client: TestClient) -> None:
    response = post_invoice(client, "customer_id=1003&sku=NOPE&qty=1")
    assert response.status_code == 200
    assert response.json() == {"error": "UNKNOWN_SKU", "sku": "NOPE"}


# --- /customer/{id}/tier and routing ---------------------------------------- #

def test_known_and_unknown_customer_tiers(client: TestClient) -> None:
    assert client.get("/customer/1001/tier").json() == {"customer_id": "1001", "tier": "GOLD"}
    assert client.get("/customer/9999/tier").json() == {"customer_id": "9999", "tier": "BRONZE"}


def test_non_numeric_customer_is_plain_text_404(client: TestClient) -> None:
    assert_text(client.get("/customer/abc/tier"), 404, "not found")


def test_unknown_path_is_plain_text_404_not_framework_json(client: TestClient) -> None:
    assert_text(client.get("/nope"), 404, "not found")


def test_get_invoice_is_plain_text_405(client: TestClient) -> None:
    assert_text(client.get("/invoice"), 405, "method not allowed")


def test_error_bodies_are_never_framework_json(client: TestClient) -> None:
    for response in (client.get("/nope"), client.get("/invoice"), client.get("/customer/abc/tier")):
        with pytest.raises(json.JSONDecodeError):
            json.loads(response.text)
