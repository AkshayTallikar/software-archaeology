# How this rebuild was made

`main.py` was written by a coding agent (a small, fast model) that was given only three files from `archaeology demo --system library`: the rebuild brief, the inferred spec and the 1,115 recorded traces. It also had the `archaeology replay` command. It never saw the JavaScript source, the legacy documentation or the answer key, and it never ran Node.js.

It reached 1,115 / 1,115 equivalent responses on its first replay. The differential probe then sent it 3,000 fresh requests and 300 fresh multi-step scenario runs, side by side with the real Node.js service, and found no divergence.

## Open questions, answered by new evidence

The four questions this section used to list were put to the old system: 11 new scenarios and one sweep were added to `legacy_library/workload.json`, the service was recorded again (1,564 exchanges, 449 of them new), and the answers come from those traces. Run numbers below are from that recording. Where the rebuild disagrees with the old system, the note says so. The divergences from the replay and the probe are listed in full, with their scenario and run, in `DIVERGENCES.md` at the root of the repository, and a later commit fixes them.

**1. Borrowing block: total fees, or one loan 20 days late?**
The old system blocks on the total of `fees_due`, at 5.00 or more. Lateness of a single loan does not matter.
- `fees-two-late` run 191: two loans, fees 2.00 + 2.00 = 4.00. The third borrow is allowed.
- `fees-two-late` run 192: fees 2.00 + 3.00 = 5.00, and no single loan is 20 days late. The third borrow is refused with `403 FEES_OUTSTANDING`.
- `fees-block` run 168 (fee 4.75, one loan 19 days late) is allowed. Run 169 (fee 5.00, 20 days late) is refused.
- `unreturned-overdue` runs 204 to 206: a loan still out and past due never blocks, whatever the date of the next borrow.
The rebuild matches all of these.

**2. Do fees on several late loans add up?**
Yes, as a plain sum. `fees-two-late` runs 187 to 201 show `fees_due` equal to the two fees added, and `fees-three-late` run 202 gives 3.00 + 3.00 + 3.00 = `9.00`. The 10.00 cap is per loan: `fees-cap` run 203 has two loans each at the cap and `fees_due` of `20.00`.
The fee schedule itself is also settled by the existing traces: a loan up to 2 days late is free. From 3 days late, the fee is 0.25 per whole day late, capped at 10.00 per loan, which is reached at 40 days late. `LEGACY_API.md` says 0.50 per day with no cap, and claims L7 and L8 in `claims.json` are contradicted by the traces. The rebuild matches the fee schedule.

**3. Check order in `POST /loans`.**
The old system checks, in this order: member exists, `borrowed_on` is a valid date, book exists, no fees outstanding, loan limit not reached, book available. Each adjacent pair was tested on its own:
- Unknown member before a bad date or a bad ISBN: `order-date-vs-isbn` and the unknown-member sweep, `MEMBER_NOT_FOUND` every time.
- Bad date before a missing book: `order-date-vs-isbn` runs 207 to 221, `400 INVALID_DATE` for unknown, malformed and available ISBNs alike.
- Missing book before fees: `order-fees-vs-isbn-date` run 222, `404 BOOK_NOT_FOUND`.
- Bad date before fees: `order-fees-vs-isbn-date` run 223, `400 INVALID_DATE`.
- Fees before availability: `order-fees-vs-unavailable` run 231, `403 FEES_OUTSTANDING` where the book is also on loan to someone else.
- Missing book before the loan limit: `order-limit` runs 232 to 237.
- Loan limit before availability: `order-limit` run 238, `409 LOAN_LIMIT` for a book on loan to someone else.
- Bad date before availability: `order-unavailable-vs-date` runs 241 and 242, `400 INVALID_DATE`.
The order between fees and the loan limit cannot be tested. A member can only reach the fee block by returning a loan, and that return takes them below the limit, so the two conditions never hold at once and both orders give the same answer.
The rebuild now matches this pair: it checks the loan limit before availability, so it answers `409 LOAN_LIMIT` as the old system does (`t-001471`, `order-limit` run 238).

**4. Error responses for invalid inputs.**
- An invalid `borrowed_on` (`2026-02-30`, `2026-13-01`, `yesterday`, empty, `05-01-2026`) returns `400 INVALID_DATE`. Evidence: `order-date-vs-isbn` runs 207 to 221.
- An invalid `returned_on` (the same values) returns `400 INVALID_DATE`, and the loan stays active. Evidence: `return-invalid-date` runs 244 to 248.
- An unknown ISBN when borrowing returns `404 BOOK_NOT_FOUND`. Malformed ISBNs do the same: `abc`, empty, 12 or 14 digits, and a hyphenated number that is not in the catalogue. Evidence: `borrow-bad-isbn` runs 250 to 255.
- A `returned_on` earlier than `borrowed_on` returns `400 RETURN_BEFORE_BORROW`, which the notes did not cover. Evidence: `return-invalid-date` run 249. The rebuild now matches this too: it refuses the return with the same error and leaves the loan active (`t-001550`, `t-001551`).

**Also found while answering these.** A loan that has not been returned shows only its own fields in the old system: `returned_on`, `days_late` and `fee` appear once the loan is returned. The rebuild also shows them on active loans, as `null`, `0` and `"0.00"`. The rebuild now matches this too: an active loan omits those fields, and a returned loan shows them after `status` (`t-001526`, `t-000133`).

**Scenario defects, and one left as it is.** The Thin evidence table flagged the join scenario's literal `{{tier}}`. That was an engine defect: a null `vary` value now leaves its field out of the request (`archaeology/scenarios.py`). `fees-block` step 4 is a cleanup return of the second loan. It is unbound in the 13 runs where the second borrow is refused, and that is correct: the block itself is step 3. Removing the step would leave the second book on loan in the 20 runs where the borrow succeeds, and every later run that borrows it would then see the wrong answer. The engine has no conditional steps, so the same cleanup pattern is used in the new block scenarios, and their unbound cleanup returns are flagged in Thin evidence.

## How strong is the proof?

`tests/test_library_rebuild.py` changes one business rule of the rebuild by a small amount at a time (fee cap 10.00 → 9.75, grace days 2 → 3, fee step 0.25 → 0.30, block threshold 5.00 → 5.25, loan limit 5 → 4, page size cap 50 → 49) and checks that replay fails each time.
