"""Behavioural comparison of two responses.

An empty list from :func:`compare` means the actual response is equivalent
to the expected one. Every difference is returned as a readable line.
"""

from __future__ import annotations

import json
from typing import Any, Collection

from archaeology.trace import Response

SNIPPET_LEN = 120
NON_JSON_SNIPPET_LEN = 80
_ROOT = "body"


def _parse(body: str) -> tuple[bool, Any]:
    """Return ``(True, value)`` if ``body`` is JSON, else ``(False, None)``."""
    if not body:
        return False, None
    try:
        return True, json.loads(body)
    except ValueError:
        return False, None


def _kind(value: Any) -> str:
    # bool must be checked before number: bool is a subclass of int.
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _value_diff(path: str, expected: Any, actual: Any) -> str:
    return f"{path}: expected {json.dumps(expected)}, got {json.dumps(actual)}"


def _walk(
    expected: Any,
    actual: Any,
    path: str,
    ignore_path: str,
    ignore: Collection[str],
    diffs: list[str],
) -> None:
    """Structurally compare two JSON values, appending differences to ``diffs``.

    ``path`` is the display path (``body.order.lines[1].qty``). ``ignore_path``
    is the dotted path used for ``ignore`` lookups, with list indexes left out
    so that ``line.sku`` matches the ``sku`` of every element of ``line``.
    """
    if ignore_path and ignore_path in ignore:
        return
    kind = _kind(expected)
    if kind != _kind(actual):
        diffs.append(_value_diff(path, expected, actual))
        return
    if kind == "dict":
        for key in sorted(expected):
            child_path = f"{path}.{key}"
            if key not in actual:
                diffs.append(f"{child_path}: missing in actual")
                continue
            child_ignore = f"{ignore_path}.{key}" if ignore_path else key
            _walk(expected[key], actual[key], child_path, child_ignore, ignore, diffs)
        for key in sorted(actual):
            if key not in expected:
                diffs.append(f"{path}.{key}: unexpected in actual")
    elif kind == "list":
        if len(expected) != len(actual):
            diffs.append(
                f"{path}: expected {len(expected)} items, got {len(actual)}"
            )
        for i, (exp_item, act_item) in enumerate(zip(expected, actual)):
            _walk(exp_item, act_item, f"{path}[{i}]", ignore_path, ignore, diffs)
    elif expected != actual:
        # Same kind here, so 1 == 1.0 holds and True/1 never reach this branch.
        diffs.append(_value_diff(path, expected, actual))


def compare(
    expected: Response,
    actual: Response,
    ignore: Collection[str] = frozenset(),
) -> list[str]:
    """Return every difference between ``expected`` and ``actual``.

    Checks, in order: status, content type (media type only), then the body.
    JSON bodies are compared structurally; ``ignore`` holds dotted paths
    (without the ``body.`` prefix) whose values are not compared. For an
    ignored path the key must still exist on both sides. Other bodies are
    compared as text with trailing whitespace stripped.
    """
    diffs: list[str] = []

    if expected.status != actual.status:
        diffs.append(f"status: expected {expected.status}, got {actual.status}")

    if expected.content_type != actual.content_type:
        diffs.append(
            f"content-type: expected '{expected.content_type}', "
            f"got '{actual.content_type}'"
        )

    # _parse accepts "null", so an expected "null" body counts as JSON.
    expected_is_json, expected_value = _parse(expected.body)

    if expected_is_json:
        actual_is_json, actual_value = _parse(actual.body)
        if not actual_is_json:
            snippet = actual.body[:NON_JSON_SNIPPET_LEN]
            diffs.append(f"body: expected JSON, got non-JSON '{snippet}'")
        else:
            _walk(
                expected_value,
                actual_value,
                _ROOT,
                "",
                frozenset(ignore),
                diffs,
            )
    else:
        exp_text = expected.body.rstrip()
        act_text = actual.body.rstrip()
        if exp_text != act_text:
            diffs.append(
                f"body: expected '{exp_text[:SNIPPET_LEN]}', "
                f"got '{act_text[:SNIPPET_LEN]}'"
            )

    return diffs
