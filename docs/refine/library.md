# Refinement log

The recording held 1821 exchanges and 18 evidence gaps. Refinement ran 1 round(s), recorded 14 additional exchanges, acted on 3 gap(s) and left 16 gap(s).

Closed means the gap no longer appears after its additions. Explained means the additions ran and the gap remains; the outcome quotes what they saw.

## Gaps before and after

| Kind | Before | After |
|---|---:|---:|
| inexact_change | 0 | 0 |
| few_values | 3 | 1 |
| single_status | 1 | 1 |
| failed_capture | 14 | 14 |
| unobserved_claim | 0 | 0 |

## Round 1

| Kind | Where | Gap | Addition | Requests | Outcome |
|---|---|---|---|---:|---|
| few_values | POST /members parameter name | only 'Ada', 'Oz' seen, across 4 requests | one request with name set to 'refine-invalid' | 1 | closed; its requests drew 201 |
| few_values | scenario loan-limit variable tier | only 'premium', 'standard' seen, across 24 requests | one run of scenario loan-limit with tier set to 'refine-invalid' | 12 | closed; its requests drew 201 x6, 409, 200 x5 |
| single_status | GET /books | only HTTP 200 seen, across 80 requests | without per_page | 1 | explained: only HTTP 200 seen, across 81 requests; its requests drew 200 |

## Not acted on

| Kind | Where | Gap | Reason | Outcome |
|---|---|---|---|---|
| few_values | scenario fee-blocked-and-free/a variable returned_on | only '2026-01-21', '2026-02-20' seen, across 50 requests | fee-blocked-and-free/a is a lane of an interleaving, not a scenario template refine can vary | not acted on |
| failed_capture | scenario borrow-return-borrow/a step 2 (POST /loans/{{loan}}/return) | 2 of 6 runs sent {{loan}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario borrow-return-borrow/b step 2 (POST /loans/{{first}}/return) | 1 of 6 runs sent {{first}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario borrow-return-borrow/b step 4 (POST /loans/{{second}}/return) | 1 of 6 runs sent {{second}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fee-blocked-and-free/a step 2 (POST /loans/{{loan}}/return) | 2 of 10 runs sent {{loan}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fee-blocked-and-free/a step 4 (POST /loans/{{later}}/return) | 4 of 10 runs sent {{later}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fee-blocked-and-free/b step 2 (POST /loans/{{loan}}/return) | 2 of 10 runs sent {{loan}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fees-block step 4 (POST /loans/{{loan2}}/return) | 13 of 33 runs sent {{loan2}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fees-three-late step 9 (POST /loans/{{p}}/return) | 1 of 1 runs sent {{p}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario fees-two-late step 7 (POST /loans/{{c}}/return) | 10 of 16 runs sent {{c}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario join step 1 (GET /members/{{member}}) | 6 of 18 runs sent {{member}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario limit-and-another-member/a step 12 (POST /loans/{{p4}}/return) | 4 of 4 runs sent {{p4}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario limit-and-another-member/b step 2 (POST /loans/{{q1}}/return) | 3 of 4 runs sent {{q1}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario same-copy/a step 3 (POST /loans/{{loan}}/return) | 1 of 5 runs sent {{loan}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |
| failed_capture | scenario same-copy/b step 3 (POST /loans/{{loan}}/return) | 2 of 5 runs sent {{loan}} unbound, because an earlier step did not capture it | a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap | not acted on |

## Gaps left after refinement

| Kind | Where | Detail |
|---|---|---|
| few_values | scenario fee-blocked-and-free/a variable returned_on | only '2026-01-21', '2026-02-20' seen, across 50 requests |
| single_status | GET /books | only HTTP 200 seen, across 81 requests |
| failed_capture | scenario borrow-return-borrow/a step 2 (POST /loans/{{loan}}/return) | 2 of 6 runs sent {{loan}} unbound, because an earlier step did not capture it |
| failed_capture | scenario borrow-return-borrow/b step 2 (POST /loans/{{first}}/return) | 1 of 6 runs sent {{first}} unbound, because an earlier step did not capture it |
| failed_capture | scenario borrow-return-borrow/b step 4 (POST /loans/{{second}}/return) | 1 of 6 runs sent {{second}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fee-blocked-and-free/a step 2 (POST /loans/{{loan}}/return) | 2 of 10 runs sent {{loan}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fee-blocked-and-free/a step 4 (POST /loans/{{later}}/return) | 4 of 10 runs sent {{later}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fee-blocked-and-free/b step 2 (POST /loans/{{loan}}/return) | 2 of 10 runs sent {{loan}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fees-block step 4 (POST /loans/{{loan2}}/return) | 13 of 33 runs sent {{loan2}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fees-three-late step 9 (POST /loans/{{p}}/return) | 1 of 1 runs sent {{p}} unbound, because an earlier step did not capture it |
| failed_capture | scenario fees-two-late step 7 (POST /loans/{{c}}/return) | 10 of 16 runs sent {{c}} unbound, because an earlier step did not capture it |
| failed_capture | scenario join step 1 (GET /members/{{member}}) | 6 of 18 runs sent {{member}} unbound, because an earlier step did not capture it |
| failed_capture | scenario limit-and-another-member/a step 12 (POST /loans/{{p4}}/return) | 4 of 4 runs sent {{p4}} unbound, because an earlier step did not capture it |
| failed_capture | scenario limit-and-another-member/b step 2 (POST /loans/{{q1}}/return) | 3 of 4 runs sent {{q1}} unbound, because an earlier step did not capture it |
| failed_capture | scenario same-copy/a step 3 (POST /loans/{{loan}}/return) | 1 of 5 runs sent {{loan}} unbound, because an earlier step did not capture it |
| failed_capture | scenario same-copy/b step 3 (POST /loans/{{loan}}/return) | 2 of 5 runs sent {{loan}} unbound, because an earlier step did not capture it |

## Notes

- The workload is never modified. The refined workload appends each addition to its own section, so a fresh run of it sends the same requests, but in a different order.
- Additions were recorded after the whole original workload, as the traces show. A stateful system therefore saw them in that order.
