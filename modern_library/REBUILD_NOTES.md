# How this rebuild was made

`main.py` was written by a coding agent (a small, fast model) that was given only three files from `archaeology demo --system library`: the rebuild brief, the inferred spec and the 1,115 recorded traces. It also had the `archaeology replay` command. It never saw the JavaScript source, the legacy documentation or the answer key, and it never ran Node.js.

It reached 1,115 / 1,115 equivalent responses on its first replay. The differential probe then sent it 3,000 fresh requests and 300 fresh multi-step scenario runs, side by side with the real Node.js service, and found no divergence.

## What the evidence left open

The agent had to decide these itself, because the recording does not separate the alternatives:

- **Borrowing block:** whether a member is blocked when outstanding fees reach 5.00, or when a loan is 20 or more days late. Both fit the recording. The agent chose the fee amount.
- **Several late loans:** whether fees on several late loans add up. The recording never has two late loans for one member. The agent assumed a plain sum.
- **Check order in `POST /loans`:** only "unknown member is checked first" is evidenced. The rest of the order is a choice.
- **Inputs never recorded:** an invalid date returns `400 INVALID_DATE`, and an unknown ISBN when borrowing returns `404 BOOK_NOT_FOUND`. Neither error code appears in the traces.

These are exactly the questions to put to the old system next: add a scenario for each to the workload, record again, and replay. That is how the engine is meant to be used: the evidence decides, not the rebuilder.

## How strong is the proof?

`tests/test_library_rebuild.py` changes one business rule of the rebuild by a small amount at a time (fee cap 10.00 → 9.75, grace days 2 → 3, fee step 0.25 → 0.30, block threshold 5.00 → 5.25, loan limit 5 → 4, page size cap 50 → 49) and checks that replay fails each time.
