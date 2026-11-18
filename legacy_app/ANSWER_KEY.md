# ANSWER KEY: ACME Billing planted behaviours

Ground truth for scoring the engine. Rebuild agents must never read this file.

Each row is a behaviour of the running system (`legacy_app/billing.py`) that is
either missing from `LEGACY_API.md` or contradicts it.

| # | Area | True behaviour | Docs say | Status |
|---|------|----------------|----------|--------|
| 1 | /quote unknown SKU | HTTP 200, JSON `{"error": "UNKNOWN_SKU", "sku": <normalised>}` | HTTP 404 | Contradicted (C1) |
| 2 | SKU case and whitespace | Case-insensitive and whitespace-trimmed (`" widget "` is `WIDGET`) | Case-sensitive | Contradicted (C2) |
| 3 | Bulk discount, 101 to 499 units | 10% when qty > 100, so 101 gets 10% and 100 gets 0% | 10% at 100 or more | Contradicted (C3) |
| 4 | Bulk discount, 500 or more units | 15% (undocumented second tier) | Not documented | Missing |
| 5 | Totals | Truncated toward zero to cents (ROUND_DOWN), not rounded | Nearest cent | Contradicted (C4) |
| 6 | Coupon and bulk ordering | Coupon applied first, then bulk on the reduced amount. No intermediate rounding, so the result equals either order. Not observable from traffic | Not documented | Missing |
| 7 | Invalid, expired or empty coupon | Silently ignored: HTTP 200, `coupon` is null, no discount, no error | INVALID_COUPON with HTTP 400 | Contradicted (C5) |
| 8 | Coupon case and whitespace | Case-insensitive and whitespace-trimmed (`" save10 "` is `SAVE10`) | Not documented | Missing |
| 9 | qty above 1000 | Clamped silently to 1000 (echoed `qty` is 1000) | Rejected with HTTP 400 | Contradicted (C8) |
| 10 | qty below 1 | HTTP 200, JSON `{"error": "QTY_MUST_BE_POSITIVE", "qty": <n>}` | Not documented | Missing |
| 11 | qty not an integer | HTTP 400 text/plain `bad qty`. Empty string also gives `bad qty` | Not documented | Missing |
| 12 | Missing sku | HTTP 400 text/plain `missing sku` | Not documented | Missing |
| 13 | Empty sku | Counts as present: HTTP 200 UNKNOWN_SKU with `"sku": ""` | Not documented | Missing |
| 14 | qty default | qty is 1 when omitted | qty defaults to 1 | Confirmed (C7) |
| 15 | Repeated query or form keys | Last value wins | Not documented | Missing |
| 16 | Unknown customer tier | BRONZE, HTTP 200. No 404 | HTTP 404 | Contradicted (C6) |
| 17 | Tier endpoint, non-numeric id | HTTP 404 text/plain `not found` | Not documented | Missing |
| 18 | Tier endpoint, numeric id | Always 200 JSON, BRONZE for unknown ids | Not documented | Missing |
| 19 | GOLD discount | 5% extra off invoice total | 5% extra | Confirmed (C9) |
| 20 | SILVER discount | 2% off invoice total | Not documented | Missing |
| 21 | Invoice tier discount rounding | Pricing total is truncated to cents first, then the tier discount is applied and truncated again | Not documented | Missing |
| 22 | Invoice number | Sequential `INV-%06d` per app instance, starting at 1. Not consumed by any error response | Sequential | Confirmed, with the error case missing |
| 23 | Volatile fields | `invoice_id` (counter) and `created_at` (UTC now) differ between identical requests | Not documented | Missing |
| 24 | Invoice customer_id | Whitespace-trimmed before tier lookup and echo. Empty string counts as present | Not documented | Missing |
| 25 | Invoice missing customer_id | HTTP 400 text/plain `missing customer_id` | Not documented | Missing |
| 26 | Invoice pricing errors | Returned unchanged (text or JSON), no invoice number consumed | Not documented | Missing |
| 27 | /invoice non-POST | HTTP 405 text/plain `method not allowed` (GET included) | Only POST accepted | Confirmed |
| 28 | Non-GET on /quote | Not routed: falls through to HTTP 404 text/plain `not found` | Not documented | Missing |
| 29 | Unknown route | HTTP 404 text/plain `not found` | Not documented | Missing |
| 30 | Error content types | Text errors are text/plain. JSON success and JSON error bodies use application/json. JSON keys are sorted | JSON | Partly documented |
| 31 | Money format | Strings, two decimals, always from Decimal | Two decimals | Confirmed |
