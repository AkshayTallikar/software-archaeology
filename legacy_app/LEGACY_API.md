ACME Billing API v2.3
=====================

Internal reference. Billing platform team, rev 4. Last edited 2011-03-14.

Base URL: http://billing.acme.internal:8001

All money values are returned as strings with exactly two decimal places,
e.g. "4.99". Do not parse them as floats.

Responses are application/json unless noted. Error responses that are not
JSON are text/plain.


1. GET /quote
-------------

Price a line item.

Query parameters:

    sku     required   product code (WIDGET, GADGET, GIZMO, DOOHICKEY)
    qty     optional   whole number of units. Defaults to 1 when omitted.
    coupon  optional   promotion code

Successful response (200):

    {
      "bulk_discount_pct": 0,
      "coupon": null,
      "qty": 3,
      "sku": "WIDGET",
      "subtotal": "14.97",
      "total": "14.97",
      "unit_price": "4.99"
    }

Errors:

    missing sku                    400 text/plain
    qty not an integer             400 text/plain  "bad qty"
    qty less than 1                200 JSON {"error": "QTY_MUST_BE_POSITIVE", "qty": <n>}
    unknown SKU                    404  (see claim C1)

Bulk pricing: orders of 100 or more units receive a 10% bulk discount
(bulk_discount_pct = 10). See claim C3.

Unknown SKUs return HTTP 404 with the error body
{"error": "UNKNOWN_SKU", "sku": "<sku>"}. See claim C1.

SKUs are case-sensitive. "WIDGET" is a valid SKU; "widget" is not. See
claim C2.

Totals are rounded to the nearest cent. See claim C4.

Coupons: a valid coupon reduces the subtotal by its percentage. Invalid or
expired coupon codes return error INVALID_COUPON with HTTP 400. See
claim C5.

Quantities above 1000 are rejected with HTTP 400. See claim C8.


2. POST /invoice
----------------

Create an invoice for one line item. Invoice numbers are issued
sequentially (INV-000001, INV-000002, ...). A request that fails validation
does not consume an invoice number.

Form fields (application/x-www-form-urlencoded):

    customer_id   required   customer account number
    sku           required   as for /quote
    qty           optional   as for /quote, default 1
    coupon        optional   as for /quote

Successful response (200):

    {
      "created_at": "2011-03-14T09:12:44+00:00",
      "customer_id": "1001",
      "invoice_id": "INV-000001",
      "line": {
        "bulk_discount_pct": 0,
        "coupon": null,
        "qty": 1,
        "sku": "GIZMO"
      },
      "tier": "GOLD",
      "tier_discount_pct": 5,
      "total": "141.55"
    }

Loyalty discount: GOLD customers receive an extra 5% off invoices. See claim
C9. SILVER and BRONZE customers receive the discount shown in
tier_discount_pct.

Errors: missing customer_id returns 400 text/plain "missing customer_id".
Pricing errors are returned exactly as described under /quote.

Only POST is accepted. Any other method returns 405 text/plain
"method not allowed".


3. GET /customer/<id>/tier
--------------------------

Return the loyalty tier for a customer account.

Successful response (200):

    {"customer_id": "1001", "tier": "GOLD"}

Unknown customers return HTTP 404 from this endpoint. See claim C6.

Tiers are GOLD, SILVER and BRONZE.


Claims summary (for the 2.3 release review)
-------------------------------------------

The following behaviours are documented for 2.3 and are believed to be
correct as of this revision:

    C1  Unknown SKUs return HTTP 404.
    C2  SKUs are case-sensitive; "widget" is not a valid SKU.
    C3  Orders of 100 or more units receive a 10% bulk discount.
    C4  Totals are rounded to the nearest cent.
    C5  Invalid or expired coupon codes return error INVALID_COUPON.
    C6  Unknown customers return HTTP 404 from the tier endpoint.
    C7  qty defaults to 1 when omitted.
    C8  Quantities above 1000 are rejected with HTTP 400.
    C9  GOLD customers receive an extra 5% off invoices.

Questions to the billing platform team.
