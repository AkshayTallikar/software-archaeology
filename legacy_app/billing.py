"""ACME Billing v2.3 -- quotes, invoices, loyalty tiers.

Plain WSGI, no framework, stdlib only. Money is decimal.Decimal throughout.
Routing is by hand, see application(). Tables live at module level.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from urllib.parse import parse_qs

# sku -> unit price (string, so Decimal gets it exact)
PRICES = {"WIDGET": "4.99", "GADGET": "19.95", "GIZMO": "149.00", "DOOHICKEY": "0.35"}

# coupon -> percent off
COUPONS = {"SAVE10": 10, "VIP25": 25}
EXPIRED = {"SUMMER21"}  # old promo, no longer in COUPONS; kept for the 2021 reports

TIERS = {"1001": "GOLD", "1002": "SILVER", "1003": "GOLD", "1004": "SILVER"}
TIER_DISCOUNT = {"GOLD": 5, "SILVER": 2, "BRONZE": 0}

CENT = Decimal("0.01")

TEXT = "text/plain"
JSON = "application/json"


def _txt(msg, status="200 OK"):
    return (status, TEXT, msg)


def _js(obj):
    return ("200 OK", JSON, json.dumps(obj, sort_keys=True))


def _last(qs):
    # last value wins for repeated keys; blank values count as present
    d = parse_qs(qs, keep_blank_values=True)
    return dict((k, v[-1]) for k, v in d.items())


def price(sku_raw, qty_raw, coupon_raw):
    """Shared by /quote and /invoice. Returns (err, res); err is a response tuple or None."""
    if sku_raw is None:
        return _txt("missing sku", "400 Bad Request"), None

    sku = sku_raw.strip().upper()

    if qty_raw is None:
        qty = 1
    else:
        try:
            qty = int(qty_raw.strip())
        except ValueError:
            return _txt("bad qty", "400 Bad Request"), None

    if sku not in PRICES:
        return _js({"error": "UNKNOWN_SKU", "sku": sku}), None

    if qty < 1:
        return _js({"error": "QTY_MUST_BE_POSITIVE", "qty": qty}), None

    # big orders get clamped, not refused
    if qty > 1000:
        qty = 1000

    unit = Decimal(PRICES[sku])
    subtotal = (unit * qty).quantize(CENT, rounding=ROUND_DOWN)
    amount = unit * qty

    code = coupon_raw.strip().upper() if coupon_raw else ""
    applied = None
    # unknown / expired / empty coupons: no error, just no discount
    if code in COUPONS:
        amount = amount * (100 - COUPONS[code]) / 100
        applied = code

    # bulk is applied on top of the coupon amount
    if qty >= 500:
        bulk = 15
    elif qty > 100:
        bulk = 10
    else:
        bulk = 0
    amount = amount * (100 - bulk) / 100

    total = amount.quantize(CENT, rounding=ROUND_DOWN)

    res = {
        "sku": sku,
        "qty": qty,
        "unit_price": PRICES[sku],
        "subtotal": str(subtotal),
        "coupon": applied,
        "bulk_discount_pct": bulk,
        "total": str(total),
    }
    return None, res


def make_app():
    """Return a fresh WSGI app with its own invoice counter."""
    state = {"next_invoice": 1}

    def quote(qs):
        err, res = price(qs.get("sku"), qs.get("qty"), qs.get("coupon"))
        if err is not None:
            return err
        return _js(res)

    def invoice(environ):
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length).decode("utf-8", "replace") if length else ""
        f = _last(raw)

        cid = f.get("customer_id")
        if cid is None:
            return _txt("missing customer_id", "400 Bad Request")

        err, res = price(f.get("sku"), f.get("qty"), f.get("coupon"))
        if err is not None:
            # error answers do not take an invoice number
            return err

        cid = cid.strip()
        tier = TIERS.get(cid, "BRONZE")
        pct = TIER_DISCOUNT[tier]
        total = (Decimal(res["total"]) * (100 - pct) / 100).quantize(CENT, rounding=ROUND_DOWN)

        inv_id = "INV-%06d" % state["next_invoice"]
        state["next_invoice"] += 1

        return _js({
            "invoice_id": inv_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "customer_id": cid,
            "tier": tier,
            "tier_discount_pct": pct,
            "line": {
                "sku": res["sku"],
                "qty": res["qty"],
                "coupon": res["coupon"],
                "bulk_discount_pct": res["bulk_discount_pct"],
            },
            "total": str(total),
        })

    def application(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "")
        parts = path.split("/")
        qs = _last(environ.get("QUERY_STRING", ""))

        if path == "/quote" and method == "GET":
            status, ctype, body = quote(qs)

        elif path == "/invoice":
            if method != "POST":
                status, ctype, body = _txt("method not allowed", "405 Method Not Allowed")
            else:
                status, ctype, body = invoice(environ)

        elif method == "GET" and parts[1:2] == ["customer"] and len(parts) == 4 and parts[3] == "tier":
            cid = parts[2]
            if cid and all(c in "0123456789" for c in cid):
                status, ctype, body = _js({"customer_id": cid, "tier": TIERS.get(cid, "BRONZE")})
            else:
                status, ctype, body = _txt("not found", "404 Not Found")

        else:
            status, ctype, body = _txt("not found", "404 Not Found")

        start_response(status, [("Content-Type", ctype)])
        return [body.encode("utf-8")]

    return application


application = make_app()


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    httpd = make_server("", 8001, application)
    print("ACME Billing listening on 8001")
    httpd.serve_forever()
