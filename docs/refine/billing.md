# Refinement log

The recording held 3226 exchanges and 9 evidence gaps. Refinement ran 1 round(s), recorded 78 additional exchanges, acted on 8 gap(s) and left 2 gap(s).

Closed means the gap no longer appears after its additions. Explained means the additions ran and the gap remains; the outcome quotes what they saw.

## Gaps before and after

| Kind | Before | After |
|---|---:|---:|
| inexact_change | 7 | 0 |
| few_values | 1 | 0 |
| single_status | 1 | 2 |
| failed_capture | 0 | 0 |
| unobserved_claim | 0 | 0 |

## Round 1

| Kind | Where | Gap | Addition | Requests | Outcome |
|---|---|---|---|---:|---|
| inexact_change | GET /quote (coupon=SAVE10, sku=DOOHICKEY; 5 more contexts), qty | bulk_discount_pct changes somewhere between sampled 100 and 105; 4 inputs between them were never sent | sweep of qty at 101, 102, 103, 104 in 6 context(s) | 24 | closed; its requests drew 200 x24 |
| inexact_change | GET /quote (coupon=SAVE10, sku=DOOHICKEY; 5 more contexts), qty | bulk_discount_pct changes somewhere between sampled 495 and 500; 4 inputs between them were never sent | sweep of qty at 496, 497, 498, 499 in 6 context(s) | 24 | closed; its requests drew 200 x24 |
| inexact_change | GET /quote (sku=GADGET), qty | bulk_discount_pct changes somewhere between sampled 98 and 105; 6 inputs between them were never sent | sweep of qty at 99, 100, 101, 102 and more in 1 context(s) | 6 | closed; its requests drew 200 x6 |
| inexact_change | GET /quote (sku=GADGET), qty | bulk_discount_pct changes somewhere between sampled 497 and 504; 6 inputs between them were never sent | sweep of qty at 498, 499, 500, 501 and more in 1 context(s) | 6 | closed; its requests drew 200 x6 |
| inexact_change | GET /quote (sku=GADGET), qty | subtotal changes somewhere between sampled 994 and 1001; 6 inputs between them were never sent | sweep of qty at 995, 996, 997, 998 and more in 1 context(s) | 6 | closed; its requests drew 200 x6 |
| inexact_change | GET /quote (sku=GADGET), qty | shape changes somewhere between sampled 0 and 3; 2 inputs between them were never sent | sweep of qty at 1, 2 in 1 context(s) | 2 | closed; its requests drew 200 x2 |
| few_values | POST /invoice parameter coupon | only 'SAVE10', 'VIP25' seen, across 436 requests | one request with coupon set to 'SUMMER21' | 1 | closed; its requests drew 200 |
| single_status | GET /customer/{id}/tier | only HTTP 200 seen, across 7 requests | unknown customer_id 8942, 1315; invalid customer_id abc | 3 | explained: only HTTP 200 seen, across 9 requests; its requests drew 200 x2, 404 |

## Not acted on

| Kind | Where | Gap | Reason | Outcome |
|---|---|---|---|---|
| single_status | GET /customer/abc/tier | only HTTP 404 seen, across 2 requests | nothing to add: no unsampled input, id, parameter or value to vary | not acted on |

## Gaps left after refinement

| Kind | Where | Detail |
|---|---|---|
| single_status | GET /customer/abc/tier | only HTTP 404 seen, across 2 requests |
| single_status | GET /customer/{id}/tier | only HTTP 200 seen, across 9 requests |

## Notes

- The workload is never modified. The refined workload appends each addition to its own section, so a fresh run of it sends the same requests, but in a different order.
- Additions were recorded after the whole original workload, as the traces show. A stateful system therefore saw them in that order.
