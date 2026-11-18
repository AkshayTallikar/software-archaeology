"""Boundaries: how outputs change as a numeric input is swept (steps, shapes, clamps)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..trace import Trace
from .catalog import group_by_endpoint, json_object, value_type


def _as_int(value: Any) -> int | None:
    """The value as an int if it converts to one (int or integer string), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value_type(value) == "int":
        return int(value)
    return None


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _label(endpoint: str, context: tuple[tuple[str, str], ...]) -> str:
    if not context:
        return endpoint
    pairs = ", ".join(f"{k}={v}" for k, v in context)
    return f"{endpoint} ({pairs})"


def _shape(obj: dict[str, Any] | None, status: int) -> str:
    if obj is None:
        return f"{status} text"
    return f"{status} " + ",".join(sorted(obj))


def _segments(points: Sequence[tuple[int, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """Contiguous runs of equal values along a sorted sweep, and where values change."""
    segments: list[dict[str, Any]] = []
    changes: list[int] = []
    for x, value in points:
        if segments and _key(segments[-1]["value"]) == _key(value):
            segments[-1]["to"] = x
        else:
            if segments:
                changes.append(x)
            segments.append({"value": value, "from": x, "to": x})
    return segments, changes


def _clamp(points: Sequence[tuple[int, Any]]) -> dict[str, Any] | None:
    """Detect an echoed input that stops following the input (a clamp), or None."""
    pairs = [(x, _as_int(out)) for x, out in points]
    if all(x == out for x, out in pairs):
        return None
    best_len, best_end, run = 0, -1, 0
    for i, (x, out) in enumerate(pairs):
        run = run + 1 if x == out else 0
        if run > best_len:
            best_len, best_end = run, i
    if best_len == 0 or best_end == len(pairs) - 1:
        return None
    later = {out for _, out in pairs[best_end + 1 :]}
    then = next(iter(later)) if len(later) == 1 else None
    return {"echo_until": pairs[best_end][0], "then": then}


def _finding(
    endpoint: str,
    param: str,
    field: str,
    kind: str,
    context: tuple[tuple[str, str], ...],
    description: str,
    **extra: Any,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "endpoint": endpoint,
        "param": param,
        "field": field,
        "context": dict(context),
        "kind": kind,
        "description": description,
    }
    finding.update(extra)
    return finding


def _sweep_findings(
    endpoint: str,
    param: str,
    context: tuple[tuple[str, str], ...],
    points: Sequence[tuple[int, Trace]],
    min_points: int,
    max_levels: int,
) -> list[dict[str, Any]]:
    label = _label(endpoint, context)
    objects = [json_object(t.response) for _, t in points]
    findings: list[dict[str, Any]] = []

    shape_points = [
        (x, _shape(obj, t.response.status)) for (x, t), obj in zip(points, objects)
    ]
    segments, changes = _segments(shape_points)
    if len(segments) >= 2:
        parts = ", ".join(
            f"'{s['value']}' for {param} {s['from']}-{s['to']}" for s in segments
        )
        findings.append(
            _finding(
                endpoint, param, "shape", "shape", context,
                f"{label}: response shape is {parts}",
                segments=segments, changes_at=changes,
            )
        )

    fields: dict[str, list[tuple[int, Any]]] = {}
    for (x, _), obj in zip(points, objects):
        if obj is None:
            continue
        for name, value in obj.items():
            if _is_scalar(value):
                fields.setdefault(name, []).append((x, value))

    for name in sorted(fields):
        pts = fields[name]
        if len(pts) < min_points:
            continue
        if name == param and all(_as_int(v) is not None for _, v in pts):
            clamp = _clamp(pts)
            if clamp is not None:
                then = clamp["then"]
                tail = f"then stays at {then}" if then is not None else "then no longer does"
                findings.append(
                    _finding(
                        endpoint, param, name, "clamp", context,
                        f"{label}: {name} follows the input up to {clamp['echo_until']}, {tail}",
                        echo_until=clamp["echo_until"], then=then,
                    )
                )
            continue
        distinct = {_key(v) for _, v in pts}
        if 2 <= len(distinct) <= max_levels and len(pts) >= 2 * len(distinct):
            segments, changes = _segments(pts)
            parts = ", ".join(
                f"{s['value']} for {param} {s['from']}-{s['to']}" for s in segments
            )
            findings.append(
                _finding(
                    endpoint, param, name, "step", context,
                    f"{label}: {name} is {parts}",
                    segments=segments, changes_at=changes,
                )
            )
    return findings


def _sweeps(
    group: Sequence[Trace], param: str, min_points: int
) -> list[tuple[tuple[tuple[str, str], ...], list[tuple[int, Trace]]]]:
    """Per other-parameter context, the first trace per distinct integer value of ``param``."""
    buckets: dict[tuple[tuple[str, str], ...], list[tuple[int, Trace]]] = {}
    for trace in group:
        raw = trace.request.params.get(param)
        if raw is None or value_type(raw) != "int":
            continue
        context = tuple(sorted((k, v) for k, v in trace.request.params.items() if k != param))
        buckets.setdefault(context, []).append((int(raw), trace))

    sweeps = []
    for context in sorted(buckets):
        first_per_value: dict[int, Trace] = {}
        for x, trace in sorted(buckets[context], key=lambda item: item[0]):
            first_per_value.setdefault(x, trace)
        points = sorted(first_per_value.items())
        if len(points) >= min_points:
            sweeps.append((context, points))
    return sweeps


def _payload(finding: dict[str, Any]) -> Any:
    if "segments" in finding:
        return finding["segments"]
    return [finding["echo_until"], finding["then"]]


def _order(finding: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        finding["endpoint"],
        finding["param"],
        json.dumps(finding["context"], sort_keys=True),
        finding["field"],
    )


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one finding per identical result across contexts, counting the contexts."""
    kept: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in sorted(findings, key=_order):
        signature = (
            finding["endpoint"],
            finding["param"],
            finding["field"],
            finding["kind"],
            json.dumps(_payload(finding), sort_keys=True),
        )
        if signature in kept:
            kept[signature]["contexts"] += 1
        else:
            finding["contexts"] = 1
            kept[signature] = finding
    return list(kept.values())


def detect_boundaries(
    traces: Sequence[Trace], min_points: int = 8, max_levels: int = 6
) -> list[dict[str, Any]]:
    """Find response shape changes, step functions and clamps along integer input sweeps.

    Output is sorted by (endpoint, param, context, field) and deduplicated across
    contexts with a ``contexts`` count.
    """
    findings: list[dict[str, Any]] = []
    for endpoint, group in sorted(group_by_endpoint(traces).items()):
        names = sorted({name for t in group for name in t.request.params})
        for param in names:
            for context, points in _sweeps(group, param, min_points):
                findings.extend(
                    _sweep_findings(endpoint, param, context, points, min_points, max_levels)
                )
    return _dedupe(findings)
