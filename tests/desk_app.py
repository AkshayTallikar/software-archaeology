"""A tiny stateful WSGI lending desk with one shared copy, used to test interleavings.

Members register, borrow the single copy and return it by the loan's
identifier. Two instances with different prefixes and seeds issue different
identifiers for the same requests, as a legacy system and its rebuild do.
``bug=True`` builds a candidate that checks availability per member: a copy on
loan to one member looks free to another, which only shows when two members'
steps are interleaved.
"""

from __future__ import annotations

import json
import random
from typing import Any, Callable
from urllib.parse import parse_qsl

WSGIApp = Callable[..., Any]


def _json(start_response: Callable[..., Any], status: str, data: dict[str, Any]) -> list[bytes]:
    body = json.dumps(data).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
    return [body]


def _form(environ: dict[str, Any]) -> dict[str, str]:
    length = int(environ.get("CONTENT_LENGTH") or 0)
    raw = environ["wsgi.input"].read(length).decode("utf-8")
    return dict(parse_qsl(raw, keep_blank_values=True))


def make_desk(prefix: str, seed: int, bug: bool = False) -> WSGIApp:
    """A lending desk with fresh in-memory state whose identifiers are ``prefix`` plus 8 hex digits."""
    rng = random.Random(seed)
    members: set[str] = set()
    loans: dict[str, dict[str, Any]] = {}

    def new_id() -> str:
        return prefix + f"{rng.getrandbits(32):08x}"

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        method = environ["REQUEST_METHOD"]
        parts = environ["PATH_INFO"].strip("/").split("/")

        if method == "POST" and parts == ["members"]:
            member = new_id()
            members.add(member)
            return _json(start_response, "201 Created", {"member_id": member})

        if method == "POST" and parts == ["loans"]:
            member = _form(environ).get("member_id", "")
            if member not in members:
                return _json(start_response, "404 Not Found", {"error": "MEMBER_NOT_FOUND"})
            # The planted bug: only the borrower's own loans count as holding the copy.
            held = any(loan["active"] and (not bug or loan["member"] == member) for loan in loans.values())
            if held:
                return _json(start_response, "409 Conflict", {"error": "BOOK_UNAVAILABLE"})
            loan_id = new_id()
            loans[loan_id] = {"member": member, "active": True}
            return _json(start_response, "201 Created", {"loan_id": loan_id, "member_id": member, "status": "active"})

        if method == "POST" and len(parts) == 3 and parts[0] == "loans" and parts[2] == "return":
            loan = loans.get(parts[1])
            if loan is None:
                return _json(start_response, "404 Not Found", {"error": "LOAN_NOT_FOUND"})
            if not loan["active"]:
                return _json(start_response, "409 Conflict", {"error": "ALREADY_RETURNED"})
            loan["active"] = False
            return _json(start_response, "200 OK", {"loan_id": parts[1], "status": "returned"})

        if method == "GET" and parts == ["copy"]:
            return _json(start_response, "200 OK", {"available": not any(loan["active"] for loan in loans.values())})

        return _json(start_response, "404 Not Found", {"error": "NOT_FOUND"})

    return app
