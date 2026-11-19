"""Pinecrest Library HTTP service, rebuilt from recorded traffic.

The rebuild reproduces the recorded system exactly, including behaviour that
contradicts the old documentation. Every business rule below is a named
constant with a comment citing the evidence it was derived from.

Use ``make_app()`` to obtain a fresh application with fresh in-memory state.
Requests carry form-encoded bodies; responses are ``application/json``.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# Business rules (named constants; evidence in the comments)
# ---------------------------------------------------------------------------

# Loan period in days, by normalised tier. Standard loans borrowed on
# 2026-01-05 are due 2026-01-19 (14 days); premium loans are due 2026-02-02
# (28 days). The documented "14 days for all loans" claim is contradicted.
LOAN_PERIOD_DAYS = {"standard": 14, "premium": 28}

# Tiers are matched after trimming and lower-casing. Only "premium" is a
# known tier; any other spelling ("gold", "{{tier}}") is stored as "standard"
# and the member is still created with status 201 (tier is case-insensitive,
# unknown tiers are not rejected).
PREMIUM_TIER = "premium"
DEFAULT_TIER = "standard"

# Maximum loans a member may hold at once. The loan-limit scenario succeeds
# for five loans and refuses the sixth with 409 LOAN_LIMIT.
MAX_ACTIVE_LOANS = 5

# Late-fee schedule, from the late-return and premium-late-return scenarios.
# days_late is measured from the due date. Days 1 and 2 late are free; from
# day 3 the fee is 0.75 and rises 0.25 per further day, capped at 10.00.
# Cents are used throughout so the formatted values never drift.
FEE_FREE_DAYS = 2
FEE_FIRST_CHARGE_CENTS = 75
FEE_STEP_CENTS = 25
FEE_CAP_CENTS = 1000

# A member whose outstanding fees reach this amount may not borrow. The
# fees-block scenario sees 4.75 allow a loan and 5.00 refuse it with 403
# FEES_OUTSTANDING.
FEE_BORROW_BLOCK_CENTS = 500

# Pagination for GET /books. per_page falls back to 10 when unparseable,
# is clamped to the range 1..50 (the documented maximum of 100 is wrong),
# and page is clamped to at least 1.
PER_PAGE_DEFAULT = 10
PER_PAGE_MIN = 1
PER_PAGE_MAX = 50
PAGE_DEFAULT = 1

# Each identifier is generated as a prefix plus lowercase hex digits. The
# format is what clients rely on; the values themselves are not compared.
MEMBER_ID_PREFIX = "m_"
MEMBER_ID_HEX_DIGITS = 8
LOAN_ID_PREFIX = "L-"
LOAN_ID_HEX_DIGITS = 6

# Seed catalogue: the sixty titles the library holds, keyed by 13-digit ISBN.
# These titles are the values returned by the recorded GET /books listings
# and by POST /loans responses.
CATALOG = (
    ("9780000000019", "Gilded Atlas"),
    ("9780000000026", "Forgotten Orchard"),
    ("9780000000033", "Distant Meadow"),
    ("9780000000040", "Crimson Compass"),
    ("9780000000057", "Quiet Parlour"),
    ("9780000000064", "Hollow Garden"),
    ("9780000000071", "Windward Ledger"),
    ("9780000000088", "Amber Cartographer"),
    ("9780000000095", "Restless Lantern"),
    ("9780000000101", "Silent Chronicle"),
    ("9780000000118", "Gilded Almanac"),
    ("9780000000125", "Forgotten Harbour"),
    ("9780000000132", "Distant Atlas"),
    ("9780000000149", "Crimson Orchard"),
    ("9780000000156", "Quiet Meadow"),
    ("9780000000163", "Hollow Compass"),
    ("9780000000170", "Windward Parlour"),
    ("9780000000187", "Amber Garden"),
    ("9780000000194", "Restless Ledger"),
    ("9780000000200", "Silent Cartographer"),
    ("9780000000217", "Gilded Lantern"),
    ("9780000000224", "Forgotten Chronicle"),
    ("9780000000231", "Distant Almanac"),
    ("9780000000248", "Crimson Harbour"),
    ("9780000000255", "Quiet Atlas"),
    ("9780000000262", "Hollow Orchard"),
    ("9780000000279", "Windward Meadow"),
    ("9780000000286", "Amber Compass"),
    ("9780000000293", "Restless Parlour"),
    ("9780000000309", "Silent Garden"),
    ("9780000000316", "Gilded Ledger"),
    ("9780000000323", "Forgotten Cartographer"),
    ("9780000000330", "Distant Lantern"),
    ("9780000000347", "Crimson Chronicle"),
    ("9780000000354", "Quiet Almanac"),
    ("9780000000361", "Hollow Harbour"),
    ("9780000000378", "Windward Atlas"),
    ("9780000000385", "Amber Orchard"),
    ("9780000000392", "Restless Meadow"),
    ("9780000000408", "Silent Compass"),
    ("9780000000415", "Gilded Parlour"),
    ("9780000000422", "Forgotten Garden"),
    ("9780000000439", "Distant Ledger"),
    ("9780000000446", "Crimson Cartographer"),
    ("9780000000453", "Quiet Lantern"),
    ("9780000000460", "Hollow Chronicle"),
    ("9780000000477", "Windward Almanac"),
    ("9780000000484", "Amber Harbour"),
    ("9780000000491", "Restless Atlas"),
    ("9780000000507", "Silent Orchard"),
    ("9780000000514", "Gilded Meadow"),
    ("9780000000521", "Forgotten Compass"),
    ("9780000000538", "Distant Parlour"),
    ("9780000000545", "Crimson Garden"),
    ("9780000000552", "Quiet Ledger"),
    ("9780000000569", "Hollow Cartographer"),
    ("9780000000576", "Windward Lantern"),
    ("9780000000583", "Amber Chronicle"),
    ("9780000000590", "Restless Almanac"),
    ("9780000000606", "Silent Harbour"),
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_LEADING_INT = re.compile(r"\s*[+-]?\d+")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISBN_SEPARATORS = re.compile(r"[\s-]+")


def parse_leading_int(raw: Optional[str], default: int) -> int:
    """Parse an integer the way the recorded server does (parseInt-like).

    Leading digits are taken and anything after them is ignored, so "5abc"
    is 5 and "7.9" is 7. Input with no leading digits (empty, "abc") gives
    the default. Sign is honoured, so "-3" is -3 before clamping.
    """
    if raw is None:
        return default
    match = _LEADING_INT.match(raw)
    if not match:
        return default
    return int(match.group(0))


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def normalise_isbn(raw: str) -> str:
    """Remove hyphens and whitespace, which the server ignores in ISBNs.

    Surrounding whitespace is trimmed by the same rule, since it is
    whitespace too.
    """
    return _ISBN_SEPARATORS.sub("", raw)


def parse_iso_date(raw: Optional[str]) -> Optional[date]:
    """Parse a YYYY-MM-DD date, returning None when it is not a real date."""
    if raw is None or not _ISO_DATE.fullmatch(raw.strip()):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def normalise_tier(raw: str) -> str:
    """Map a submitted tier onto a stored tier (case-insensitive, trimmed)."""
    return PREMIUM_TIER if raw.strip().lower() == PREMIUM_TIER else DEFAULT_TIER


def fee_cents_for(days_late: int) -> int:
    """Late fee in cents for a given number of days late (see FEE_* above)."""
    if days_late <= FEE_FREE_DAYS:
        return 0
    charge = FEE_FIRST_CHARGE_CENTS + FEE_STEP_CENTS * (days_late - FEE_FREE_DAYS - 1)
    return min(charge, FEE_CAP_CENTS)


def money(cents: int) -> str:
    """Format cents as a two-decimal string such as "0.75" or "10.00"."""
    return f"{cents // 100}.{cents % 100:02d}"


def utc_timestamp() -> str:
    """ISO-8601 UTC timestamp with millisecond precision and a Z suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Member:
    member_id: str
    name: str
    tier: str
    created_at: str


@dataclass
class Loan:
    loan_id: str
    member_id: str
    isbn: str
    borrowed_on: date
    due_on: date
    returned_on: Optional[date] = None
    days_late: int = 0
    fee_cents: int = 0

    @property
    def active(self) -> bool:
        return self.returned_on is None


class Library:
    """All mutable state for one app instance."""

    def __init__(self) -> None:
        self.titles: Dict[str, str] = dict(CATALOG)
        self.members: Dict[str, Member] = {}
        self.loans: Dict[str, Loan] = {}

    # identifiers -----------------------------------------------------------
    def new_member_id(self) -> str:
        while True:
            candidate = MEMBER_ID_PREFIX + secrets.token_hex(MEMBER_ID_HEX_DIGITS // 2)
            if candidate not in self.members:
                return candidate

    def new_loan_id(self) -> str:
        while True:
            candidate = LOAN_ID_PREFIX + secrets.token_hex(LOAN_ID_HEX_DIGITS // 2)
            if candidate not in self.loans:
                return candidate

    # derived values --------------------------------------------------------
    def active_loans_for(self, member_id: str) -> List[Loan]:
        return [l for l in self.loans.values() if l.member_id == member_id and l.active]

    def fees_due_cents(self, member_id: str) -> int:
        return sum(
            l.fee_cents for l in self.loans.values()
            if l.member_id == member_id and not l.active
        )

    def is_available(self, isbn: str) -> bool:
        return not any(l.isbn == isbn and l.active for l in self.loans.values())


# ---------------------------------------------------------------------------
# Response serialisation
# ---------------------------------------------------------------------------

def error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def member_body(member: Member, lib: Library, sorted_keys: bool) -> dict:
    """Member JSON. POST and GET use slightly different key orders."""
    values = {
        "member_id": member.member_id,
        "name": member.name,
        "tier": member.tier,
        "active_loans": len(lib.active_loans_for(member.member_id)),
        "fees_due": money(lib.fees_due_cents(member.member_id)),
        "created_at": member.created_at,
    }
    if sorted_keys:
        return {k: values[k] for k in sorted(values)}
    return values


def loan_body(loan: Loan, title: str) -> dict:
    return {
        "loan_id": loan.loan_id,
        "member_id": loan.member_id,
        "isbn": loan.isbn,
        "title": title,
        "borrowed_on": loan.borrowed_on.isoformat(),
        "due_on": loan.due_on.isoformat(),
        "status": "active" if loan.active else "returned",
    }


def loan_detail_body(loan: Loan, title: str) -> dict:
    # An active loan shows only the loan fields. A returned loan adds the
    # return fields after status, in that order (t-000133, t-001526).
    body = loan_body(loan, title)
    if not loan.active:
        body["returned_on"] = loan.returned_on.isoformat()
        body["days_late"] = loan.days_late
        body["fee"] = money(loan.fee_cents)
    return body


def return_body(loan: Loan) -> dict:
    return {
        "loan_id": loan.loan_id,
        "returned_on": loan.returned_on.isoformat(),
        "days_late": loan.days_late,
        "fee": money(loan.fee_cents),
        "status": "returned",
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def make_app() -> FastAPI:
    """Return a new application with fresh, empty in-memory state."""
    app = FastAPI()
    lib = Library()

    async def form_params(request: Request) -> Dict[str, str]:
        """Read a form-encoded body. Blank values are kept; first value wins."""
        raw = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    # --- books -------------------------------------------------------------

    @app.get("/books")
    async def list_books(request: Request):
        query = {k: v[0] for k, v in parse_qs(request.url.query, keep_blank_values=True).items()}
        page = max(parse_leading_int(query.get("page"), PAGE_DEFAULT), 1)
        per_page = clamp(
            parse_leading_int(query.get("per_page"), PER_PAGE_DEFAULT),
            PER_PAGE_MIN, PER_PAGE_MAX,
        )
        catalogue = sorted(lib.titles.items())
        start = (page - 1) * per_page
        window = catalogue[start:start + per_page]
        items = [
            {"isbn": isbn, "title": title, "available": lib.is_available(isbn)}
            for isbn, title in window
        ]
        # Past-the-end pages are not an error: they return an empty items list.
        return JSONResponse({
            "page": page,
            "per_page": per_page,
            "total": len(catalogue),
            "items": items,
        })

    @app.get("/books/{book_id}")
    async def get_book(book_id: str):
        isbn = normalise_isbn(book_id)
        title = lib.titles.get(isbn)
        if title is None:
            return error(404, "BOOK_NOT_FOUND")
        return JSONResponse({
            "isbn": isbn,
            "title": title,
            "available": lib.is_available(isbn),
        })

    # --- members -----------------------------------------------------------

    @app.post("/members")
    async def create_member(request: Request):
        params = await form_params(request)
        name = params.get("name", "").strip()
        if not name:
            return error(400, "NAME_REQUIRED")
        member = Member(
            member_id=lib.new_member_id(),
            name=name,
            tier=normalise_tier(params.get("tier", "")),
            created_at=utc_timestamp(),
        )
        lib.members[member.member_id] = member
        # Creating a member returns 201 (not 200 as the old documentation says).
        return JSONResponse(member_body(member, lib, sorted_keys=False), status_code=201)

    @app.get("/members/{member_id}")
    async def get_member(member_id: str):
        member = lib.members.get(member_id)
        if member is None:
            return error(404, "MEMBER_NOT_FOUND")
        return JSONResponse(member_body(member, lib, sorted_keys=True))

    # --- loans -------------------------------------------------------------

    @app.post("/loans")
    async def borrow(request: Request):
        params = await form_params(request)
        member = lib.members.get(params.get("member_id", ""))
        if member is None:
            return error(404, "MEMBER_NOT_FOUND")

        borrowed_on = parse_iso_date(params.get("borrowed_on"))
        if borrowed_on is None:
            return error(400, "INVALID_DATE")

        isbn = normalise_isbn(params.get("isbn", ""))
        if isbn not in lib.titles:
            return error(404, "BOOK_NOT_FOUND")

        # Members owing fees may not borrow (fees-block scenario).
        if lib.fees_due_cents(member.member_id) >= FEE_BORROW_BLOCK_CENTS:
            return error(403, "FEES_OUTSTANDING")

        # The loan limit is checked before availability (t-001471).
        if len(lib.active_loans_for(member.member_id)) >= MAX_ACTIVE_LOANS:
            return error(409, "LOAN_LIMIT")

        if not lib.is_available(isbn):
            return error(409, "BOOK_UNAVAILABLE")

        period = LOAN_PERIOD_DAYS[member.tier]
        loan = Loan(
            loan_id=lib.new_loan_id(),
            member_id=member.member_id,
            isbn=isbn,
            borrowed_on=borrowed_on,
            due_on=borrowed_on + timedelta(days=period),
        )
        lib.loans[loan.loan_id] = loan
        return JSONResponse(loan_body(loan, lib.titles[isbn]), status_code=201)

    @app.get("/loans/{loan_id}")
    async def get_loan(loan_id: str):
        loan = lib.loans.get(loan_id)
        if loan is None:
            return error(404, "LOAN_NOT_FOUND")
        return JSONResponse(loan_detail_body(loan, lib.titles[loan.isbn]))

    @app.post("/loans/{loan_id}/return")
    async def return_loan(loan_id: str, request: Request):
        loan = lib.loans.get(loan_id)
        if loan is None:
            return error(404, "LOAN_NOT_FOUND")
        params = await form_params(request)
        returned_on = parse_iso_date(params.get("returned_on"))
        if returned_on is None:
            return error(400, "INVALID_DATE")

        # Returning a loan twice is not an error: the stored result is repeated
        # and the first returned_on is kept (the documented 409 is wrong).
        if loan.active:
            # A return dated before the borrow date is refused and the loan
            # stays active (t-001550, t-001551). A return dated on the borrow
            # date is accepted (six 200 returns in the traces have that date).
            if returned_on < loan.borrowed_on:
                return error(400, "RETURN_BEFORE_BORROW")
            loan.returned_on = returned_on
            loan.days_late = max((returned_on - loan.due_on).days, 0)
            loan.fee_cents = fee_cents_for(loan.days_late)
        return JSONResponse(return_body(loan))

    # --- fallback ----------------------------------------------------------

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def not_found(path: str):
        return error(404, "NOT_FOUND")

    return app
