"""Volatile response fields: values that change or look generated and must be ignored."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ..trace import Trace
from .catalog import group_by_endpoint, json_object

ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"
)
_SEQUENCE_STRING = re.compile(r"(\D*)([0-9]+)")


def flatten_json(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested JSON objects into ``{"a.b.c": leaf}``; lists are leaves."""
    if not isinstance(obj, dict):
        return {prefix: obj}
    flat: dict[str, Any] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten_json(value, path))
        else:
            flat[path] = value
    return flat


def _is_sequential(values: Sequence[Any]) -> bool:
    """True when distinct ids of one kind strictly increase in trace order."""
    if all(isinstance(v, str) for v in values):
        matches = [_SEQUENCE_STRING.fullmatch(v) for v in values]
        if any(m is None for m in matches):
            return False
        if len({m.group(1) for m in matches if m}) != 1:
            return False
        numbers = [int(m.group(2)) for m in matches if m]
    elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        numbers = list(values)
    else:
        return False
    if len(set(values)) != len(values):
        return False
    return all(a < b for a, b in zip(numbers, numbers[1:]))


def _is_timestamp(values: Sequence[Any]) -> bool:
    """True when every non-null value is an ISO-8601 datetime string."""
    non_null = [v for v in values if v is not None]
    return bool(non_null) and all(
        isinstance(v, str) and ISO_DATETIME.match(v) is not None for v in non_null
    )


def _varying_paths(members: Sequence[dict[str, Any]]) -> set[str]:
    """Paths whose values differ between traces that share one request."""
    varying: set[str] = set()
    for path in set().union(*(m.keys() for m in members)):
        seen = {json.dumps(m[path], sort_keys=True) for m in members if path in m}
        if len(seen) > 1:
            varying.add(path)
    return varying


def _endpoint_volatility(group: Sequence[Trace]) -> list[dict[str, str]]:
    flats = [flatten_json(json_object(t.response)) for t in group]

    by_request: dict[Any, list[dict[str, Any]]] = {}
    for trace, flat in zip(group, flats):
        by_request.setdefault(trace.request.key(), []).append(flat)
    varying: set[str] = set()
    for members in by_request.values():
        if len(members) >= 2:
            varying |= _varying_paths(members)

    values_by_path: dict[str, list[Any]] = {}
    for flat in flats:
        for path, value in flat.items():
            values_by_path.setdefault(path, []).append(value)

    findings: list[dict[str, str]] = []
    for path in sorted(values_by_path):
        values = values_by_path[path]
        if path in varying:
            reason = "varies across identical requests"
        elif len(values) >= 2 and _is_timestamp(values):
            reason = "timestamp-like"
        elif len(values) >= 3 and _is_sequential(values):
            reason = "sequential identifier"
        else:
            continue
        findings.append({"path": path, "reason": reason})
    return findings


def detect_volatile_fields(traces: Sequence[Trace]) -> dict[str, list[dict[str, str]]]:
    """Find volatile top-level and nested fields per endpoint (JSON object responses only).

    Returns ``{endpoint_key: [{"path": ..., "reason": ...}, ...]}`` for endpoints
    with findings, each list sorted by path.
    """
    objects = [t for t in traces if json_object(t.response) is not None]
    result: dict[str, list[dict[str, str]]] = {}
    for key, group in sorted(group_by_endpoint(objects).items()):
        findings = _endpoint_volatility(group)
        if findings:
            result[key] = sorted(findings, key=lambda f: f["path"])
    return result


def volatile_paths(volatile: dict[str, list[dict[str, str]]], key: str) -> set[str]:
    """The set of volatile field paths recorded for one endpoint key."""
    return {f["path"] for f in volatile.get(key, [])}
