"""Identity map: translate the recording's generated identifiers into the rebuild's.

A stateful system hands out identifiers (``n-7f3a91c2``) that differ from one
system to the next, so a recorded request such as ``GET /notes/n-7f3a91c2``
cannot be sent to a rebuild literally: the rebuild never issued that id. An
:class:`IdentityMap` learns the pairs as they appear. Whenever a field the
spec marks volatile differs between the recording and the candidate, the two
values are remembered; later requests are rewritten through the map before
they are sent, and later expected responses are rewritten before they are
compared.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from urllib.parse import quote, quote_plus

from archaeology.infer.volatility import flatten_json
from archaeology.trace import Request, Response

#: Shorter values are too likely to occur by chance to be treated as identifiers.
MIN_IDENTIFIER_LENGTH = 4


def _json_object(response: Response) -> dict | None:
    """The body as a JSON object, or ``None`` for anything else."""
    value = response.json()
    return value if isinstance(value, dict) else None


def _forms_for(mapping: dict[str, str]) -> dict[str, str]:
    """Every textual form of each mapped value, raw first, then URL-encoded.

    A raw form is never overridden by an encoded form that happens to equal it.
    """
    forms: dict[str, str] = dict(mapping)
    for recorded, rebuilt in mapping.items():
        forms.setdefault(quote_plus(recorded), quote_plus(rebuilt))
        forms.setdefault(quote(recorded, safe=""), quote(rebuilt, safe=""))
    return forms


class IdentityMap:
    """Recorded identifier -> rebuilt identifier, learned from matching responses.

    The first mapping learned for a recorded value wins; later pairs for the
    same value are ignored. Replacement is a single pass that prefers longer
    values, so a value that is a prefix of another is handled correctly and a
    replacement is never rewritten a second time.
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._forms: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None

    def __len__(self) -> int:
        return len(self._map)

    def items(self) -> list[tuple[str, str]]:
        """The learned ``(recorded, rebuilt)`` pairs, in the order they were learned."""
        return list(self._map.items())

    def learn(self, expected: Response, actual: Response, paths: Iterable[str]) -> None:
        """Remember volatile values that differ between ``expected`` and ``actual``.

        For each dotted JSON ``path`` present in both bodies whose two values are
        strings that differ, and whose expected value is at least
        :data:`MIN_IDENTIFIER_LENGTH` long, the expected value maps to the actual
        one. Non-JSON bodies are ignored.
        """
        expected_body = _json_object(expected)
        actual_body = _json_object(actual)
        if expected_body is None or actual_body is None:
            return
        expected_flat = flatten_json(expected_body)
        actual_flat = flatten_json(actual_body)
        for path in paths:
            if path not in expected_flat or path not in actual_flat:
                continue
            recorded, rebuilt = expected_flat[path], actual_flat[path]
            if not (isinstance(recorded, str) and isinstance(rebuilt, str)):
                continue
            if recorded == rebuilt or len(recorded) < MIN_IDENTIFIER_LENGTH:
                continue
            self._add(recorded, rebuilt)

    def _add(self, recorded: str, rebuilt: str) -> None:
        if recorded in self._map:
            return
        self._map[recorded] = rebuilt
        self._forms = _forms_for(self._map)
        self._pattern = None

    def _translate(self, text: str) -> str:
        if not text or not self._forms:
            return text
        if self._pattern is None:
            alternatives = sorted(self._forms, key=len, reverse=True)
            self._pattern = re.compile("|".join(re.escape(form) for form in alternatives))
        return self._pattern.sub(lambda match: self._forms[match.group(0)], text)

    def rewrite_request(self, request: Request) -> Request:
        """``request`` with every mapped identifier replaced in path, query and body.

        A request that mentions no mapped identifier is returned as the same object.
        """
        path = self._translate(request.path)
        query = self._translate(request.query_string)
        body = self._translate(request.body)
        if (path, query, body) == (request.path, request.query_string, request.body):
            return request
        return Request.build(request.method, path, query, body, request.headers)

    def rewrite_expected(self, response: Response) -> Response:
        """``response`` with every mapped identifier replaced in its body text.

        A response that mentions no mapped identifier is returned as the same object.
        """
        body = self._translate(response.body)
        if body == response.body:
            return response
        return replace(response, body=body)
