"""A small stateful WSGI notes service, used as the reference and candidate in tests.

Clients create a note, which the server answers with a generated identifier,
then read it and archive it by that identifier. Two instances with different
prefixes and seeds issue different identifiers for the same requests, exactly
as a legacy system and its rebuild do. ``bug=True`` builds a candidate that
miscounts the words of long notes when archiving them.
"""

from __future__ import annotations

import json
import random
from typing import Any, Callable
from urllib.parse import parse_qsl

WSGIApp = Callable[..., Any]

#: Archive counts are wrong only for notes longer than this (the planted bug).
BUG_THRESHOLD = 40


def _json(start_response: Callable[..., Any], status: str, data: dict[str, Any]) -> list[bytes]:
    body = json.dumps(data).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
    return [body]


def _form(environ: dict[str, Any]) -> dict[str, str]:
    length = int(environ.get("CONTENT_LENGTH") or 0)
    raw = environ["wsgi.input"].read(length).decode("utf-8")
    return dict(parse_qsl(raw, keep_blank_values=True))


def make_notes(prefix: str, seed: int, bug: bool = False) -> WSGIApp:
    """A notes app with fresh in-memory state whose identifiers are ``prefix`` plus 8 hex digits."""
    rng = random.Random(seed)
    notes: dict[str, dict[str, Any]] = {}

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        method = environ["REQUEST_METHOD"]
        parts = environ["PATH_INFO"].strip("/").split("/")

        if method == "POST" and parts == ["notes"]:
            text = _form(environ).get("text", "").strip()
            note_id = prefix + f"{rng.getrandbits(32):08x}"
            notes[note_id] = {"id": note_id, "text": text, "length": len(text), "archived": False}
            return _json(start_response, "201 Created", {"id": note_id, "text": text, "length": len(text)})

        if method == "GET" and len(parts) == 2 and parts[0] == "notes" and parts[1] in notes:
            return _json(start_response, "200 OK", notes[parts[1]])

        if method == "POST" and len(parts) == 3 and parts[0] == "notes" and parts[2] == "archive" and parts[1] in notes:
            note = notes[parts[1]]
            note["archived"] = True
            words = len(note["text"].split())
            if bug and len(note["text"]) > BUG_THRESHOLD:
                words += 1
            return _json(start_response, "200 OK", {"id": note["id"], "archived": True, "words": words})

        return _json(start_response, "404 Not Found", {"error": "NOT_FOUND"})

    return app


# Factories for `archaeology ... --reference/--candidate notes_app:make_*`.


def make_reference() -> WSGIApp:
    """The legacy system: identifiers from prefix ``n-``."""
    return make_notes("n-", 1)


def make_candidate() -> WSGIApp:
    """A correct rebuild: identifiers from prefix ``x-``."""
    return make_notes("x-", 2)


def make_buggy_candidate() -> WSGIApp:
    """A rebuild that miscounts words when archiving long notes."""
    return make_notes("x-", 2, bug=True)
