# Pinecrest Library Lending Service: API reference

Version 2.1 (branch lending desk). All endpoints accept form-encoded request
bodies and return JSON. Money is reported as a string with two decimal places.
Dates are `YYYY-MM-DD` and are calendar days in UTC.

## Conventions

* Successful responses are JSON objects with the fields listed for each endpoint.
* Errors are JSON objects of the form `{"error": "<CODE>"}`.
* An unknown route, or a known route with an unsupported method, returns
  HTTP 404 with `{"error": "NOT_FOUND"}`.
* ISBNs may be written with hyphens. Lookups are not sensitive to case.

## Members

### POST /members

Registers a new member.

Form fields:

| field  | required | notes |
|--------|----------|-------|
| name   | yes      | Leading and trailing whitespace is removed. |
| tier   | no       | `standard` or `premium`. Defaults to `standard`. Creating a member returns HTTP 200. Unknown membership tiers are rejected with 400 INVALID_TIER. |

Response: the new member object (see below).

### GET /members/{member_id}

Returns one member. Unknown members return 404 MEMBER_NOT_FOUND.

Member object:

```json
{"member_id": "m_3fa9c2d1", "name": "Ada", "tier": "standard",
 "active_loans": 0, "fees_due": "0.00", "created_at": "2026-01-05T09:12:44.512Z"}
```

## Books

### GET /books

Lists the catalogue, 60 titles, in catalogue order.

Query parameters:

| parameter | default | notes |
|-----------|---------|-------|
| page      | 1       | 1-based page number. |
| per_page  | 10      | Items per page. per_page may be up to 100. |

Response:

```json
{"page": 1, "per_page": 10, "total": 60,
 "items": [{"isbn": "9780000000019", "title": "Silent Harbour", "available": true}]}
```

Requesting a page past the end returns 404 NOT_FOUND.

### GET /books/{isbn}

Returns one title. Unknown ISBNs return 404 BOOK_NOT_FOUND.

## Loans

### POST /loans

Borrows a book for a member.

Form fields: `member_id`, `isbn`, `borrowed_on` (date).

Checks, in order:

1. The member must exist (404 MEMBER_NOT_FOUND).
2. `borrowed_on` must be a valid date (400 INVALID_DATE).
3. The book must exist (404 BOOK_NOT_FOUND).
4. Members with unpaid fees cannot borrow (403 FEES_OUTSTANDING).
5. A member may have at most 3 books on loan; a 4th loan is refused with 409 LOAN_LIMIT.
6. The book must be available (409 BOOK_UNAVAILABLE).

Response, HTTP 201:

```json
{"loan_id": "L-04b7e2", "member_id": "m_3fa9c2d1", "isbn": "9780000000019",
 "title": "Silent Harbour", "borrowed_on": "2026-01-05",
 "due_on": "2026-01-19", "status": "active"}
```

All loans are due 14 days after borrowing.

### GET /loans/{loan_id}

Returns one loan. Once returned, the loan also carries `returned_on`,
`days_late` and `fee`. Unknown loans return 404 LOAN_NOT_FOUND.

### POST /loans/{loan_id}/return

Returns a book.

Form field: `returned_on` (date).

* A `returned_on` earlier than `borrowed_on` is 400 RETURN_BEFORE_BORROW.
* Returning a loan twice returns 409 ALREADY_RETURNED.

Late fees: a return up to 2 days after the due date is free. Late returns
cost 0.50 per day from the first day late. There is no maximum late fee.
The fee is added to the member's `fees_due`.

Response:

```json
{"loan_id": "L-04b7e2", "returned_on": "2026-01-25", "days_late": 6,
 "fee": "3.00", "status": "returned"}
```

## Error codes

| code | HTTP | meaning |
|------|------|---------|
| NAME_REQUIRED | 400 | the member name is empty |
| INVALID_TIER | 400 | the membership tier is not recognised |
| INVALID_DATE | 400 | a date is missing or not a valid `YYYY-MM-DD` |
| RETURN_BEFORE_BORROW | 400 | returned before it was borrowed |
| FEES_OUTSTANDING | 403 | the member owes fees |
| MEMBER_NOT_FOUND | 404 | no such member |
| BOOK_NOT_FOUND | 404 | no such ISBN in the catalogue |
| LOAN_NOT_FOUND | 404 | no such loan |
| NOT_FOUND | 404 | no such route |
| LOAN_LIMIT | 409 | the member already has the maximum number of books on loan |
| BOOK_UNAVAILABLE | 409 | the book is on loan to someone else |
| ALREADY_RETURNED | 409 | the loan was already returned |
