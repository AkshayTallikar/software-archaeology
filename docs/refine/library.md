# Refinement log

The recording held 1564 exchanges and 8 evidence gaps. Refinement ran 1 round(s), recorded 16 additional exchanges, acted on 4 gap(s) and left 5 gap(s).

Closed means the gap no longer appears after its additions. Explained means the additions ran and the gap remains; the outcome quotes what they saw.

## Gaps before and after

| Kind | Before | After |
|---|---:|---:|
| inexact_change | 0 | 0 |
| few_values | 2 | 0 |
| single_status | 2 | 1 |
| failed_capture | 4 | 4 |
| unobserved_claim | 0 | 0 |

## Round 1

| Kind | Where | Gap | Addition | Requests | Outcome |
|---|---|---|---|---:|---|
| few_values | POST /members parameter tier | only 'gold', 'standard' seen, across 3 requests | one request with tier set to 'refine-invalid' | 1 | closed; its requests drew 201 |
| few_values | scenario loan-limit variable tier | only 'premium', 'standard' seen, across 24 requests | one run of scenario loan-limit with tier set to 'refine-invalid' | 12 | closed; its requests drew 201 x6, 409, 200 x5 |
| single_status | GET /books | only HTTP 200 seen, across 79 requests | without per_page | 1 | explained: only HTTP 200 seen, across 80 requests; its requests drew 200 |
| single_status | GET /loans/{id} | only HTTP 200 seen, across 137 requests | unknown loan_id L-2baa5f, L-d1bfe2 | 2 | closed; its requests drew 404 x2 |

## Not acted on

| Kind | Where | Gap | Reason | Outcome |
|---|---|---|---|---|
| failed_capture | scenario fees-block step 4 (POST /loans/{{loan2}}/return) | 13 of 33 runs sent {{loan2}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fees-three-late step 9 (POST /loans/{{p}}/return) | 1 of 1 runs sent {{p}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fees-two-late step 7 (POST /loans/{{c}}/return) | 10 of 16 runs sent {{c}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario join step 1 (GET /members/{{member}}) | 6 of 18 runs sent {{member}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |

## Gaps left after refinement

| Kind | Where | Detail |
|---|---|---|
| single_status | GET /books | only HTTP 200 seen, across 80 requests |
| failed_capture | scenario fees-block step 4 (POST /loans/{{loan2}}/return) | 13 of 33 runs sent {{loan2}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fees-three-late step 9 (POST /loans/{{p}}/return) | 1 of 1 runs sent {{p}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fees-two-late step 7 (POST /loans/{{c}}/return) | 10 of 16 runs sent {{c}} unbound, because an earlier step did not capture it |
| failed_capture | scenario join step 1 (GET /members/{{member}}) | 6 of 18 runs sent {{member}} unbound, because an earlier step did not capture it |

## Notes

- The workload is never modified. The refined workload appends each addition to its own section, so a fresh run of it sends the same requests, but in a different order.
- Additions were recorded after the whole original workload, as the traces show. A stateful system therefore saw them in that order.
