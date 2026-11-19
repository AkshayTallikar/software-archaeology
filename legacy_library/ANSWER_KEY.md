# ANSWER KEY: Pinecrest Library planted behaviours

Ground truth for scoring the engine. Rebuild agents must never read this file.

Each row is a behaviour of the running system (`legacy_library/server.js`) that
is either missing from `LEGACY_API.md` or contradicts it. Claims are the
entries in `claims.json`; L12 and L13 are true and contradict nothing.

| # | Area | True behaviour | Docs say | Status |
|---|------|----------------|----------|--------|
| B1 | POST /members status | HTTP 201 Created on success | HTTP 200 | Contradicted (L1) |
| B2 | Membership tier | Trimmed and case-insensitive (`" PREMIUM "` is premium). Any other value, including missing, silently becomes standard, HTTP 201 | Unknown tiers rejected with 400 INVALID_TIER | Contradicted (L2) |
| B3 | Member name | Trimmed. Blank after trimming is 400 NAME_REQUIRED | Not documented | Missing |
| B4 | Catalogue paging | per_page clamped to 1..50. page below 1 is treated as 1. A page past the end is HTTP 200 with `items: []` | per_page up to 100; page past the end is 404 | Contradicted (L3, L4) |
| B5 | Paging parameters | Parsed with JavaScript `parseInt`: `"7.9"` is 7, `"5abc"` is 5, `"abc"` and `""` fall back to the default (page 1, per_page 10) | Not documented | Missing |
| B6 | Loan due date | Standard loans are due 14 days after borrowing, premium loans 28 days after | All loans due 14 days after borrowing | Contradicted (L5) |
| B7 | Loan limit | A member may hold 5 books. The 6th loan is refused with 409 LOAN_LIMIT | Not documented as 5 (documented as 3) | Contradicted (L6) |
| B8 | Late fee | Returns up to 2 days late are free. From day 3 every late day costs 0.25 (3 days late is 0.75). The fee is capped at 10.00 | 0.50 per day from the first day, no maximum | Contradicted (L7, L8) |
| B9 | Borrowing with fees | Fees of 5.00 or more block borrowing (403 FEES_OUTSTANDING). Smaller fees do not | Members with unpaid fees cannot borrow | Contradicted (L10) |
| B10 | Returning twice | HTTP 200 with the original receipt, unchanged. No state change | 409 ALREADY_RETURNED | Contradicted (L9) |
| B11 | ISBN format | Hyphens, spaces and surrounding whitespace are ignored and letters uppercased. Applies to paths and form fields | Hyphens allowed (L11 true) | Confirmed (L11) |
| B12 | Generated values | member_id is `m_` plus 8 random hex characters, loan_id is `L-` plus 6 random hex characters, created_at is the wall-clock time of creation | Not documented | Missing |
