"""Endpoint catalog: path templates, parameters, and response shapes."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ..trace import Response, Trace

_DIGITS = re.compile(r"[0-9]+")
_FLOAT = re.compile(r"-?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?|-?\d+[eE][-+]?\d+")
_INT = re.compile(r"-?[0-9]+")
EXAMPLE_LIMIT = 5
TEXT_EXAMPLE_LIMIT = 3


def path_template(path: str) -> str:
    """Replace every all-digit path segment with ``{id}``."""
    return "/".join("{id}" if _DIGITS.fullmatch(seg) else seg for seg in path.split("/"))


def endpoint_key_for(method: str, path: str) -> str:
    """Return the endpoint key ``"METHOD /template"`` for a raw request line."""
    return f"{method} {path_template(path)}"


def endpoint_key(trace: Trace) -> str:
    """Return the endpoint key of a trace."""
    return endpoint_key_for(trace.request.method, trace.request.path)


def group_by_endpoint(traces: Sequence[Trace]) -> dict[str, list[Trace]]:
    """Group traces by endpoint key, preserving input order within each group."""
    groups: dict[str, list[Trace]] = {}
    for trace in traces:
        groups.setdefault(endpoint_key(trace), []).append(trace)
    return groups


def value_type(value: str) -> str:
    """Classify a raw parameter value as empty, int, float or str."""
    if value == "":
        return "empty"
    if _INT.fullmatch(value):
        return "int"
    # A strict pattern, not float(): float() also accepts " 1001", "nan" and
    # "inf", which are strings as far as the caller sent them.
    if _FLOAT.fullmatch(value):
        return "float"
    return "str"


def json_type(value: Any) -> str:
    """Return the JSON type name of a decoded JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    raise TypeError(f"not a JSON value: {value!r}")


def parse_body(body: str) -> tuple[bool, Any]:
    """Return ``(True, value)`` when the body is JSON, else ``(False, None)``."""
    try:
        return True, json.loads(body)
    except ValueError:
        return False, None


def json_object(response: Response) -> dict[str, Any] | None:
    """Return the body as a dict when it is a JSON object, else ``None``."""
    ok, value = parse_body(response.body)
    return value if ok and isinstance(value, dict) else None


def _distinct(values: Sequence[str], limit: int) -> list[str]:
    """First ``limit`` distinct values, in first-seen order."""
    return list(dict.fromkeys(values))[:limit]


def _param_catalog(group: Sequence[Trace]) -> dict[str, dict[str, Any]]:
    names = sorted({name for trace in group for name in trace.request.params})
    params: dict[str, dict[str, Any]] = {}
    for name in names:
        values = [t.request.params[name] for t in group if name in t.request.params]
        params[name] = {
            "types": sorted({value_type(v) for v in values}),
            "required": len(values) == len(group),
            "examples": _distinct(values, EXAMPLE_LIMIT),
        }
    return params


def _response_catalog(group: Sequence[Trace]) -> list[dict[str, Any]]:
    buckets: dict[tuple[int, str, tuple[str, ...] | None], list[tuple[Trace, dict | None]]] = {}
    for trace in group:
        obj = json_object(trace.response)
        keys = tuple(sorted(obj)) if obj is not None else None
        key = (trace.response.status, trace.response.content_type, keys)
        buckets.setdefault(key, []).append((trace, obj))

    shapes: list[dict[str, Any]] = []
    for (status, ctype, keys), members in buckets.items():
        shape: dict[str, Any] = {
            "status": status,
            "content_type": ctype,
            "count": len(members),
            "json_keys": list(keys) if keys is not None else None,
            "field_types": None,
            "text_examples": [],
        }
        if keys is not None:
            shape["field_types"] = {
                k: sorted({json_type(obj[k]) for _, obj in members if obj is not None})
                for k in keys
            }
        else:
            shape["text_examples"] = _distinct(
                [t.response.body for t, _ in members], TEXT_EXAMPLE_LIMIT
            )
        shapes.append(shape)
    shapes.sort(key=lambda s: (s["status"], s["content_type"], s["json_keys"] or []))
    return shapes


def build_catalog(traces: Sequence[Trace]) -> list[dict[str, Any]]:
    """Describe every endpoint: parameters, types, optionality and response shapes.

    Entries are sorted by (path template, method).
    """
    entries: list[dict[str, Any]] = []
    for key, group in group_by_endpoint(traces).items():
        first = group[0].request
        entries.append(
            {
                "endpoint": key,
                "method": first.method,
                "path": path_template(first.path),
                "count": len(group),
                "params": _param_catalog(group),
                "responses": _response_catalog(group),
            }
        )
    entries.sort(key=lambda e: (e["path"], e["method"]))
    return entries
