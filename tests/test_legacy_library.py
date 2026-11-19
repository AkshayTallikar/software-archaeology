"""Tests for the Pinecrest Library legacy sample (Node.js, standard library only).

Each test starts a fresh ``node legacy_library/server.js`` process, so state is
always empty. The planted behaviours are listed in legacy_library/ANSWER_KEY.md
and are asserted here through HTTP alone.
"""

import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import pytest

from archaeology.recorder import ListSink, exercise_target, expand_workload
from archaeology.replay import ProcessTarget
from archaeology.interleave import interleaving_request_count
from archaeology.scenarios import expand_scenarios
from archaeology.trace import FORM_CONTENT_TYPE, Request

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "legacy_library" / "server.js"
LIBRARY = ROOT / "legacy_library"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# Claims whose documented statement is true; every other claim is documented false.
TRUE_CLAIMS = {"L11", "L12", "L13"}

MEMBER_ID = re.compile(r"^m_[0-9a-f]{8}$")
LOAN_ID = re.compile(r"^L-[0-9a-f]{6}$")


def isbn_check_sum(digits: str) -> int:
    """ISBN-13 weighted sum (weights 1, 3, 1, 3, ...); a valid ISBN sums to a multiple of 10."""
    return sum(int(d) * (1 if k % 2 == 0 else 3) for k, d in enumerate(digits))


def isbn13(i: int) -> str:
    """Catalogue book ``i`` (1..60) with its ISBN-13 check digit."""
    core = "9780" + str(i).zfill(8)
    return core + str((10 - isbn_check_sum(core) % 10) % 10)


def hyphenate(isbn: str) -> str:
    return f"{isbn[:3]}-{isbn[3]}-{isbn[4:6]}-{isbn[6:12]}-{isbn[12]}"


def day(offset: int) -> str:
    """ISO date ``offset`` days after 2026-01-19, the standard due date of a 2026-01-05 loan."""
    return (date(2026, 1, 19) + timedelta(days=offset)).isoformat()


@pytest.fixture
def library():
    with ProcessTarget(["node", str(SERVER)]) as target:
        yield target


def get(target, path, **query):
    return target(Request.build("GET", path, query_string=urlencode(query)))


def post(target, path, **form):
    body = urlencode(form)
    return target(Request.build("POST", path, body=body, headers={"Content-Type": FORM_CONTENT_TYPE}))


def new_member(target, tier="standard", name="Ada"):
    response = post(target, "/members", name=name, tier=tier)
    assert response.status == 201
    return response.json()["member_id"]


def borrow(target, member_id, isbn, on="2026-01-05"):
    return post(target, "/loans", member_id=member_id, isbn=isbn, borrowed_on=on)


def give_back(target, loan_id, on):
    return post(target, f"/loans/{loan_id}/return", returned_on=on)


def fee_after_returning(target, days_late, isbn):
    """Borrow ``isbn`` on 2026-01-05 and return it ``days_late`` after its due date."""
    member_id = new_member(target)
    loan = borrow(target, member_id, isbn).json()
    receipt = give_back(target, loan["loan_id"], day(days_late)).json()
    return receipt


# --- B1 and B12: creation answers and generated values --------------------------------


def test_b1_creating_a_member_answers_201(library):
    assert post(library, "/members", name="Ada", tier="standard").status == 201


def test_b12_generated_ids_are_random_tokens_and_created_at_is_wall_clock(library):
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    first = post(library, "/members", name="Ada", tier="standard").json()
    second = post(library, "/members", name="Ada", tier="standard").json()
    assert MEMBER_ID.match(first["member_id"])
    assert first["member_id"] != second["member_id"]
    created = datetime.fromisoformat(first["created_at"].replace("Z", "+00:00"))
    assert started <= created <= datetime.now(timezone.utc) + timedelta(seconds=1)

    loan = borrow(library, first["member_id"], isbn13(1)).json()
    assert LOAN_ID.match(loan["loan_id"])
    fetched = get(library, f"/loans/{loan['loan_id']}").json()
    assert fetched["loan_id"] == loan["loan_id"]


# --- B2: tiers ------------------------------------------------------------------------


def test_b2_tier_is_trimmed_case_insensitive_and_unknown_tiers_become_standard(library):
    assert post(library, "/members", name="Ada", tier=" PREMIUM ").json()["tier"] == "premium"
    assert post(library, "/members", name="Ada", tier="gold").json()["tier"] == "standard"
    response = post(library, "/members", name="Ada")
    assert response.status == 201
    assert response.json()["tier"] == "standard"


# --- B3: names ------------------------------------------------------------------------


def test_b3_names_are_trimmed_and_blank_names_are_refused(library):
    member = post(library, "/members", name="  Grace  ", tier="standard").json()
    assert member["name"] == "Grace"
    blank = post(library, "/members", name="   ", tier="standard")
    assert blank.status == 400
    assert blank.json() == {"error": "NAME_REQUIRED"}


# --- B4: paging clamps ----------------------------------------------------------------


def test_b4_per_page_is_clamped_to_1_to_50(library):
    assert get(library, "/books", per_page="0").json()["per_page"] == 1
    big = get(library, "/books", per_page="80").json()
    assert big["per_page"] == 50
    assert len(big["items"]) == 50
    assert big["total"] == 60


def test_b4_page_below_one_is_page_one(library):
    body = get(library, "/books", page="0", per_page="10").json()
    assert body["page"] == 1
    assert body["items"][0]["isbn"] == isbn13(1)


def test_b4_a_page_past_the_end_is_200_with_no_items(library):
    response = get(library, "/books", page="8", per_page="10")
    assert response.status == 200
    assert response.json()["items"] == []


# --- B5: parseInt paging ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "effective"),
    [("7.9", 7), ("5abc", 5), ("abc", 10), ("", 10), ("-3", 1), ("07", 7)],
)
def test_b5_per_page_follows_parseint(library, raw, effective):
    assert get(library, "/books", per_page=raw).json()["per_page"] == effective


def test_b5_page_follows_parseint_and_missing_means_page_one(library):
    body = get(library, "/books", page="2.9", per_page="10").json()
    assert body["page"] == 2
    assert body["items"][0]["isbn"] == isbn13(11)
    assert get(library, "/books").json()["page"] == 1


# --- B6: loan periods -------------------------------------------------------------------


def test_b6_standard_loans_are_due_14_days_after_and_premium_28(library):
    standard = borrow(library, new_member(library, "standard"), isbn13(20)).json()
    premium = borrow(library, new_member(library, "premium"), isbn13(21)).json()
    assert standard["due_on"] == "2026-01-19"
    assert premium["due_on"] == "2026-02-02"


# --- B7: loan limit ---------------------------------------------------------------------


def test_b7_a_member_may_hold_five_books_and_the_sixth_is_refused(library):
    member_id = new_member(library)
    for i in range(30, 35):
        assert borrow(library, member_id, isbn13(i)).status == 201
    refused = borrow(library, member_id, isbn13(35))
    assert refused.status == 409
    assert refused.json() == {"error": "LOAN_LIMIT"}


# --- B8: late fees ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days_late", "fee"),
    [(2, "0.00"), (3, "0.75"), (39, "9.75"), (40, "10.00"), (41, "10.00")],
)
def test_b8_late_fee_has_a_two_day_grace_then_0_25_per_day_capped_at_10(library, days_late, fee):
    receipt = fee_after_returning(library, days_late, isbn13(40))
    assert receipt["days_late"] == days_late
    assert receipt["fee"] == fee


# --- B9: fees block borrowing at 5.00 and above -----------------------------------------


def test_b9_fees_below_five_do_not_block_and_five_or_more_do(library):
    owes_four_seventy_five = new_member(library)
    give_back(library, borrow(library, owes_four_seventy_five, isbn13(50)).json()["loan_id"], day(19))
    assert borrow(library, owes_four_seventy_five, isbn13(51)).status == 201

    owes_five = new_member(library)
    give_back(library, borrow(library, owes_five, isbn13(52)).json()["loan_id"], day(20))
    blocked = borrow(library, owes_five, isbn13(53))
    assert blocked.status == 403
    assert blocked.json() == {"error": "FEES_OUTSTANDING"}


# --- B10: returning twice ---------------------------------------------------------------


def test_b10_returning_a_returned_loan_gives_back_the_original_receipt(library):
    member_id = new_member(library)
    loan_id = borrow(library, member_id, isbn13(60)).json()["loan_id"]
    first = give_back(library, loan_id, "2026-01-25")
    assert first.status == 200
    second = give_back(library, loan_id, "2026-03-31")
    assert second.status == 200
    assert second.json() == first.json()
    assert second.json() == {
        "loan_id": loan_id, "returned_on": "2026-01-25", "days_late": 6, "fee": "1.50", "status": "returned",
    }
    assert get(library, f"/members/{member_id}").json()["fees_due"] == "1.50"


# --- B11: ISBN normalisation ------------------------------------------------------------


def test_b11_isbns_ignore_hyphens_and_spaces_in_paths_and_forms(library):
    plain = isbn13(11)
    assert get(library, f"/books/{hyphenate(plain)}").json()["isbn"] == plain
    assert get(library, f"/books/{plain[:3]}%20{plain[3:]}").json()["isbn"] == plain
    loan = borrow(library, new_member(library), f" {hyphenate(plain)} ")
    assert loan.status == 201
    assert loan.json()["isbn"] == plain


def test_unknown_isbn_is_404_book_not_found(library):
    response = get(library, "/books/9780000000000")
    assert response.status == 404
    assert response.json() == {"error": "BOOK_NOT_FOUND"}


# --- Catalogue and identifiers ----------------------------------------------------------


def test_every_catalogue_isbn_has_a_valid_check_digit(library):
    isbns = []
    for page in ("1", "2"):
        isbns += [item["isbn"] for item in get(library, "/books", page=page, per_page="50").json()["items"]]
    assert len(isbns) == 60
    for isbn in isbns:
        assert len(isbn) == 13 and isbn.startswith("9780")
        assert isbn_check_sum(isbn) % 10 == 0, isbn


def test_unknown_member_is_404_member_not_found(library):
    response = get(library, "/members/m_00000000")
    assert response.status == 404
    assert response.json() == {"error": "MEMBER_NOT_FOUND"}


# --- Whole-workload recording and claims ------------------------------------------------


def _matches_claim(spec, trace):
    if "scenario" in spec:
        tag = trace.scenario
        if not tag or tag["name"] != spec["scenario"] or tag["step"] != spec["step"]:
            return False
        return all(tag["vars"].get(k) == v for k, v in spec.get("vars", {}).items())
    if trace.scenario is not None:
        return False
    params = spec.get("query", spec.get("form", {}))
    return (
        trace.request.method == spec["method"]
        and trace.request.path == spec["path"]
        and trace.request.params == {k: str(v) for k, v in params.items()}
    )


def _claim_holds(expect, trace):
    if "status" in expect and trace.response.status != expect["status"]:
        return False
    if "json_subset" in expect:
        body = trace.response.json() or {}
        return all(body.get(k) == v for k, v in expect["json_subset"].items())
    return True


def test_recorded_workload_matches_every_claim():
    workload = json.loads((LIBRARY / "workload.json").read_text(encoding="utf-8"))
    claims = json.loads((LIBRARY / "claims.json").read_text(encoding="utf-8"))
    assert {c["id"] for c in claims} == {f"L{i}" for i in range(1, 14)}

    sink = ListSink()
    with ProcessTarget(["node", str(SERVER)]) as target:
        count = exercise_target(target, workload, sink)

    expected = (
        len(expand_workload(workload))
        + sum(len(run.steps) for run in expand_scenarios(workload))
        + interleaving_request_count(workload)
    )
    assert count == expected == len(sink)
    assert all(trace.response.status != 500 for trace in sink)

    for claim in claims:
        matched = [trace for trace in sink if _matches_claim(claim["request"], trace)]
        assert matched, f"{claim['id']} matches no recorded trace"
        holds = [_claim_holds(claim["expect"], trace) for trace in matched]
        if claim["id"] in TRUE_CLAIMS:
            assert all(holds), f"{claim['id']} is documented true but does not hold"
        else:
            assert not any(holds), f"{claim['id']} is documented false but holds"
