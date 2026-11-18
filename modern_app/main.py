"""ACME Billing, rebuilt as a FastAPI service.

The behaviour reproduced here is the observed behaviour of the running system,
as recorded in ``traces.jsonl`` (1,495 exchanges). Where the old documentation
disagrees with the traffic, the traffic wins. Every quirk is documented next to
the code that preserves it, with the evidence that established it.

Design notes
------------
* Requests are routed by one catch-all handler. FastAPI's own 404/405 bodies
  would be JSON, which the legacy system never returns, so they never reach the
  client: every response is built explicitly here.
* Query strings and form bodies are parsed by hand with ``parse_qs`` and
  ``keep_blank_values=True``. Typed FastAPI parameters would answer 422 for
  inputs the legacy system accepts (missing or blank values, odd numbers).
* All money is ``Decimal``. Each discount step truncates to the cent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Final
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

# --------------------------------------------------------------------------- #
# Observed catalogue, coupons, tiers and volume discounts
# --------------------------------------------------------------------------- #

CENT: Final = Decimal("0.01")

# Unit prices observed in every successful /quote and /invoice response.
CATALOGUE: Final[dict[str, Decimal]] = {
    "WIDGET": Decimal("4.99"),
    "GADGET": Decimal("19.95"),
    "GIZMO": Decimal("149.00"),
    "DOOHICKEY": Decimal("0.35"),
}

# Coupon percentages. Only these two codes change prices. Anything else
# (SUMMER21, BOGUS, ...) is accepted silently and echoed back as coupon: null.
# Evidence: qty=3&sku=WIDGET&coupon=VIP25 -> total 11.22 (25% off 14.97).
COUPONS: Final[dict[str, int]] = {
    "SAVE10": 10,
    "VIP25": 25,
}

# Customer tiers observed from GET /customer/{id}/tier and the POST /invoice
# responses. Any other customer id is BRONZE with a 0% discount (e.g. 5000).
@dataclass(frozen=True)
class Tier:
    name: str
    discount_pct: int


BRONZE: Final = Tier("BRONZE", 0)
SILVER: Final = Tier("SILVER", 2)
GOLD: Final = Tier("GOLD", 5)

TIERS: Final[dict[str, Tier]] = {
    "1001": GOLD,
    "1002": SILVER,
    "1003": GOLD,
    "1004": SILVER,
    "2000": BRONZE,
    "5000": BRONZE,
    "9999": BRONZE,
}

# Quantities above this are silently clamped to it, for both the quote and the
# invoice. Evidence: qty=1001 and qty=1500 for WIDGET both return qty 1000 and a
# subtotal of 4990.00. The documentation's "rejected with HTTP 400" is wrong.
MAX_QTY: Final = 1000


# --------------------------------------------------------------------------- #
# Pure pricing functions
# --------------------------------------------------------------------------- #

def bulk_discount_pct(qty: int) -> int:
    """Volume discount percentage for a (clamped) quantity.

    Evidence: WIDGET is 0% for qty 1-100, 10% for 101-499, 15% for 500-1000
    (every recorded point). The documentation says 10% from qty 100; the traffic
    shows 0% at exactly 100. GADGET matches the same thresholds where observed
    (0% at 98, 10% at 105, 15% at 504). The 99-104 and 498-503 gaps are not
    observed for GADGET and are assumed to match WIDGET.
    """
    if qty >= 500:
        return 15
    if qty >= 101:
        return 10
    return 0


def percent_off(amount: Decimal, pct: int) -> Decimal:
    """Apply a percentage discount and truncate to the cent.

    Evidence: the legacy system truncates rather than rounds. 0.35 with SAVE10
    gives 0.31 (not 0.32, the documented nearest-cent result). 14.97 with VIP25
    gives 11.22 (11.2275 truncated). Truncation happens after every step.
    """
    return (amount * (100 - pct) / 100).quantize(CENT, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class Pricing:
    """Result of pricing a line before any customer tier is applied."""

    subtotal: Decimal
    bulk_pct: int
    coupon: str | None
    total: Decimal


def normalise_coupon(raw: str | None) -> str | None:
    """Return the canonical coupon code, or None when it is not a live code.

    Evidence: coupon is case-insensitive and whitespace-trimmed (11 case pairs
    and 11 whitespace pairs gave identical responses). Blank or unknown codes
    are ignored, not rejected.
    """
    if raw is None:
        return None
    code = raw.strip().upper()
    return code if code in COUPONS else None


def price_line(unit: Decimal, qty: int, coupon: str | None) -> Pricing:
    """Price a line in the legacy order: subtotal, bulk, coupon.

    Evidence: customer 1001, WIDGET x150, VIP25 gives 479.96. That is 673.65
    after 10% bulk, then 505.23 after 25% coupon, then 479.96 after the 5%
    GOLD tier. Coupon-before-bulk gives 479.97, which is not observed.
    """
    subtotal = (unit * qty).quantize(CENT)
    bulk = bulk_discount_pct(qty)
    total = percent_off(subtotal, bulk)
    if coupon is not None:
        total = percent_off(total, COUPONS[coupon])
    return Pricing(subtotal=subtotal, bulk_pct=bulk, coupon=coupon, total=total)


def tier_for(customer_id: str) -> Tier:
    """Customer tier, defaulting to BRONZE for unknown customers."""
    return TIERS.get(customer_id, BRONZE)


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #

Params = dict[str, list[str]]

_INT_PATTERN: Final = re.compile(r"[+-]?[0-9]+")
_CUSTOMER_TIER_PATH: Final = re.compile(r"/customer/([^/]*)/tier")


class BadQuantity(ValueError):
    """Raised when qty is present but is not a whole number."""


def first_value(params: Params, key: str) -> str | None:
    """First value for a key, or None when the key is absent.

    Blank values are kept (``keep_blank_values``), so ``sku=`` is distinct
    from a missing ``sku``. Repeated keys resolve to their first value.
    """
    values = params.get(key)
    return values[0] if values else None


def parse_qty(raw: str | None) -> int:
    """Parse qty. Absent means 1. Present but not a whole number is an error.

    Evidence: no qty param gives qty 1 (sku-only quotes). qty=abc, qty= (blank)
    and qty=2.5 all give 400 'bad qty'.
    """
    if raw is None:
        return 1
    text = raw.strip()
    if not _INT_PATTERN.fullmatch(text):
        raise BadQuantity(raw)
    return int(text)


def normalise_sku(raw: str | None) -> str | None:
    """Canonical SKU, or None when missing or blank.

    Evidence: sku is case-insensitive and whitespace-trimmed (widget, ' Widget ',
    'GADGET  ' all price as the catalogue entry).
    """
    if raw is None or not raw.strip():
        return None
    return raw.strip().upper()


# --------------------------------------------------------------------------- #
# Response helpers (every body is built explicitly)
# --------------------------------------------------------------------------- #

def json_response(payload: dict[str, object], status: int = 200) -> Response:
    """JSON body with default separators, matching the legacy output byte for byte."""
    return Response(content=json.dumps(payload), status_code=status, media_type="application/json")


def text_response(message: str, status: int) -> Response:
    """Plain-text body. The content type is set exactly, with no charset suffix."""
    return Response(content=message, status_code=status, headers={"content-type": "text/plain"})


# --------------------------------------------------------------------------- #
# Invoice numbering
# --------------------------------------------------------------------------- #

class InvoiceNumbers:
    """Sequential invoice ids, INV-000001 onwards.

    Evidence: the id advances only for successful invoices. The UNKNOWN_SKU and
    'bad qty' responses do not consume a number (INV-000004 is followed by
    INV-000005 after an error).
    """

    def __init__(self) -> None:
        self._last = 0

    def next_id(self) -> str:
        self._last += 1
        return f"INV-{self._last:06d}"


# --------------------------------------------------------------------------- #
# Endpoint handlers
# --------------------------------------------------------------------------- #

def handle_quote(params: Params) -> Response:
    """GET /quote.

    Evidence for the status codes and bodies:
    * missing sku -> 400 text/plain 'missing sku'
    * bad qty -> 400 text/plain 'bad qty'
    * unknown sku -> 200 JSON {"error": "UNKNOWN_SKU", "sku": ...}; the docs
      claim 404, the traffic shows 200
    * qty <= 0 -> 200 JSON {"error": "QTY_MUST_BE_POSITIVE", "qty": ...}
    """
    sku = normalise_sku(first_value(params, "sku"))
    if sku is None:
        return text_response("missing sku", 400)
    try:
        qty = parse_qty(first_value(params, "qty"))
    except BadQuantity:
        return text_response("bad qty", 400)
    if sku not in CATALOGUE:
        return json_response({"error": "UNKNOWN_SKU", "sku": sku})
    if qty <= 0:
        return json_response({"error": "QTY_MUST_BE_POSITIVE", "qty": qty})

    qty = min(qty, MAX_QTY)
    coupon = normalise_coupon(first_value(params, "coupon"))
    pricing = price_line(CATALOGUE[sku], qty, coupon)
    return json_response(
        {
            "bulk_discount_pct": pricing.bulk_pct,
            "coupon": pricing.coupon,
            "qty": qty,
            "sku": sku,
            "subtotal": str(pricing.subtotal),
            "total": str(pricing.total),
            "unit_price": str(CATALOGUE[sku].quantize(CENT)),
        }
    )


def handle_invoice(params: Params, invoices: InvoiceNumbers, now: Callable[[], datetime]) -> Response:
    """POST /invoice (form-encoded).

    Evidence for the status codes and bodies:
    * blank or missing customer_id -> 400 text/plain 'missing customer_id'
    * bad qty -> 400 text/plain 'bad qty' (the traces show this with a valid customer)
    * unknown sku -> 200 JSON error body; no invoice number is consumed
    * customer_id is whitespace-trimmed ('+1001' arrives as ' 1001' and is 1001)
    """
    customer_id = (first_value(params, "customer_id") or "").strip()
    if not customer_id:
        return text_response("missing customer_id", 400)
    sku = normalise_sku(first_value(params, "sku"))
    if sku is None:
        return text_response("missing sku", 400)
    try:
        qty = parse_qty(first_value(params, "qty"))
    except BadQuantity:
        return text_response("bad qty", 400)
    if sku not in CATALOGUE:
        return json_response({"error": "UNKNOWN_SKU", "sku": sku})
    if qty <= 0:
        return json_response({"error": "QTY_MUST_BE_POSITIVE", "qty": qty})

    qty = min(qty, MAX_QTY)
    coupon = normalise_coupon(first_value(params, "coupon"))
    pricing = price_line(CATALOGUE[sku], qty, coupon)
    tier = tier_for(customer_id)
    total = percent_off(pricing.total, tier.discount_pct)
    return json_response(
        {
            "created_at": now().isoformat(timespec="microseconds"),
            "customer_id": customer_id,
            "invoice_id": invoices.next_id(),
            "line": {
                "bulk_discount_pct": pricing.bulk_pct,
                "coupon": pricing.coupon,
                "qty": qty,
                "sku": sku,
            },
            "tier": tier.name,
            "tier_discount_pct": tier.discount_pct,
            "total": str(total),
        }
    )


def handle_customer_tier(customer_id: str) -> Response:
    """GET /customer/{id}/tier.

    Evidence: unknown but numeric customers return 200 BRONZE (the docs claim
    404). A non-numeric id such as 'abc' returns 404 text/plain 'not found'.
    """
    if not customer_id.isdigit():
        return text_response("not found", 404)
    return json_response({"customer_id": customer_id, "tier": tier_for(customer_id).name})


# --------------------------------------------------------------------------- #
# Routing and application factory
# --------------------------------------------------------------------------- #

METHOD_NOT_ALLOWED = "method not allowed"
NOT_FOUND = "not found"
ROUTED_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


async def route(request: Request) -> Response:
    """Dispatch every request by path and method, with no framework 404/405.

    Evidence: GET /invoice -> 405 text/plain 'method not allowed'. Unknown
    paths (GET /nope) -> 404 text/plain 'not found'.
    """
    path = request.url.path
    method = request.method

    if path == "/quote":
        if method != "GET":
            return text_response(METHOD_NOT_ALLOWED, 405)
        return handle_quote(parse_qs(request.url.query, keep_blank_values=True))

    if path == "/invoice":
        if method != "POST":
            return text_response(METHOD_NOT_ALLOWED, 405)
        body = (await request.body()).decode("utf-8", errors="replace")
        return handle_invoice(
            parse_qs(body, keep_blank_values=True),
            request.app.state.invoices,
            lambda: datetime.now(timezone.utc),
        )

    match = _CUSTOMER_TIER_PATH.fullmatch(path)
    if match:
        customer_id = match.group(1)
        if not customer_id.isdigit():
            return text_response(NOT_FOUND, 404)
        if method != "GET":
            return text_response(METHOD_NOT_ALLOWED, 405)
        return handle_customer_tier(customer_id)

    return text_response(NOT_FOUND, 404)


def create_app() -> FastAPI:
    """Build a fresh app with its own invoice counter (INV-000001 onwards)."""
    app = FastAPI(
        title="ACME Billing",
        description="Quote, invoice and customer-tier API rebuilt from recorded traffic.",
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.invoices = InvoiceNumbers()

    app.add_api_route("/{path:path}", route, methods=ROUTED_METHODS)

    @app.exception_handler(StarletteHTTPException)
    async def plain_text_errors(_: Request, exc: StarletteHTTPException) -> Response:
        """Framework-raised 404/405 become the legacy plain-text bodies."""
        message = NOT_FOUND if exc.status_code == 404 else str(exc.detail)
        return text_response(message, exc.status_code)

    return app


app = create_app()

def make_app() -> FastAPI:
    """Zero-argument factory for the replay loader, which calls make_* functions.

    Each replay therefore starts with a fresh invoice counter.
    """
    return create_app()
