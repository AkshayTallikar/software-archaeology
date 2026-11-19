"""Boundaries: how outputs change as an input is swept.

Four kinds of finding come out of a sweep:

* ``shape``: the response shape (status and JSON keys) changes at some input.
* ``step``: a field takes a few discrete values, each over a contiguous range.
* ``clamp``: an echoed input stops following the input (a cap).
* ``linear``: a numeric field follows a piecewise-linear formula, described by
  segments with exact decimal slopes (grace period, jump, rate, cap).

Sweeps run over two kinds of input. Stateless traces are swept per endpoint
over their request parameters. Scenario traces are swept per (scenario name,
step index) over the varied values of their run. An input may be an integer or
an ISO date; dates are analysed as day ordinals and reported as ISO dates.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ..scenarios import scenario_family
from ..trace import Trace
from .catalog import endpoint_key, group_by_endpoint, json_object, value_type

INT = "int"
DATE = "date"
_NUMBER_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def _as_int(value: Any) -> int | None:
    """The value as an int if it converts to one (int or integer string), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value_type(value) == "int":
        return int(value)
    return None


def _ordinal(value: Any, axis: str) -> int | None:
    """The sweep position of a value on an axis: an int, or a date's day ordinal."""
    if axis == DATE:
        if isinstance(value, str) and value_type(value) == "date":
            return date.fromisoformat(value).toordinal()
        return None
    return _as_int(value)


def _show(position: int, axis: str) -> int | str:
    """A sweep position as reported: the int itself, or the ISO date."""
    return date.fromordinal(position).isoformat() if axis == DATE else position


def _position(raw: str) -> tuple[str, int] | None:
    """``(axis, position)`` for a raw int or date value, else None."""
    kind = value_type(raw)
    if kind == INT:
        return INT, int(raw)
    if kind == DATE:
        return DATE, date.fromisoformat(raw).toordinal()
    return None


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _label(base: str, context: tuple[tuple[str, str], ...]) -> str:
    if not context:
        return base
    pairs = ", ".join(f"{k}={v}" for k, v in context)
    return f"{base} ({pairs})"


def _span(low: int | str, high: int | str, axis: str) -> str:
    """A reported range: ``1-96`` for ints, ``2026-01-05 to 2026-01-09`` for dates."""
    return f"{low} to {high}" if axis == DATE else f"{low}-{high}"


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


def _present(
    segments: list[dict[str, Any]], changes: list[int], axis: str
) -> tuple[list[dict[str, Any]], list[int | str]]:
    """Convert segment bounds and change points from positions to reported values."""
    shown = [{**s, "from": _show(s["from"], axis), "to": _show(s["to"], axis)} for s in segments]
    return shown, [_show(c, axis) for c in changes]


def _clamp(points: Sequence[tuple[int, Any]], axis: str) -> dict[str, Any] | None:
    """Detect an echoed input that stops following the input (a clamp), or None."""
    pairs = [(x, _ordinal(out, axis)) for x, out in points]
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
    return {
        "echo_until": _show(pairs[best_end][0], axis),
        "then": None if then is None else _show(then, axis),
    }


# --- linear segments ---------------------------------------------------------


@dataclass
class _Run:
    """A contiguous run of points: indices of its first and last point, and its slope.

    ``delta`` is ``(change in value, change in position)`` of the pair that set
    the run's slope, or None for a run with a single point.
    """

    first: int
    last: int
    delta: tuple[Decimal, int] | None


def _decimal(value: Any) -> Decimal | None:
    """The exact decimal of a numeric JSON value or numeric string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str) and _NUMBER_TEXT.fullmatch(value):
        return Decimal(value)
    return None


def _same_slope(a: tuple[Decimal, int], b: tuple[Decimal, int]) -> bool:
    """Exact slope equality by cross-multiplication, with no division."""
    return a[0] * b[1] == b[0] * a[1]


def _linear_runs(points: Sequence[tuple[int, Decimal, str]]) -> list[_Run]:
    """Split a sorted sweep into runs of constant slope.

    The point where the slope changes starts a new run, so a jump point begins
    the next run and that run's slope is set by its next point.
    """
    runs: list[_Run] = []
    first = last = 0
    slope: tuple[Decimal, int] | None = None
    for j in range(1, len(points)):
        x0, v0, _ = points[j - 1]
        x1, v1, _ = points[j]
        pair = (v1 - v0, x1 - x0)
        if slope is None:
            slope = pair
            last = j
        elif _same_slope(slope, pair):
            last = j
        else:
            runs.append(_Run(first, last, slope))
            first = last = j
            slope = None
    runs.append(_Run(first, last, slope))
    return runs


def _plain(value: Decimal) -> str:
    """A decimal without exponent or trailing zeros: ``0.25``, ``10``, ``0``."""
    return format(value.normalize(), "f")


def _slope_text(delta: tuple[Decimal, int] | None) -> str:
    if delta is None:
        return "0"
    return _plain(delta[0] / Decimal(delta[1]))


def _segment_text(segment: dict[str, Any], unit: str) -> str:
    slope = segment["slope"]
    if slope == "0":
        if segment["from"] == segment["to"]:
            return f"{segment['start']} at {segment['from']}"
        return f"{segment['start']} from {segment['from']} to {segment['to']}"
    direction = "falling" if slope.startswith("-") else "rising"
    return (
        f"{segment['start']} at {segment['from']} {direction} {slope.lstrip('-')} per {unit} "
        f"to {segment['end']} at {segment['to']}"
    )


def _linear_finding(
    sweep: _Sweep, name: str, pts: Sequence[tuple[int, Any]], max_levels: int
) -> dict[str, Any] | None:
    """A ``linear`` finding for one numeric field along a sweep, or None."""
    numbers: list[tuple[int, Decimal, str]] = []
    for x, value in pts:
        number = _decimal(value)
        if number is None:
            return None
        numbers.append((x, number, str(value)))
    if len(numbers) < 3:
        return None
    if all(_ordinal(value, sweep.axis) == x for x, value in pts):
        return None  # an echo of the input, which clamp analysis owns

    runs = _linear_runs(numbers)
    if not 2 <= len(runs) <= max_levels:
        return None
    if all(run.delta is None or run.delta[0] == 0 for run in runs):
        return None

    segments = []
    for run in runs:
        x0, _, start = numbers[run.first]
        x1, _, end = numbers[run.last]
        segments.append(
            {
                "from": _show(x0, sweep.axis),
                "to": _show(x1, sweep.axis),
                "start": start,
                "end": end,
                "slope": _slope_text(run.delta),
            }
        )
    unit = "day" if sweep.axis == DATE else sweep.param
    text = "; ".join(_segment_text(s, unit) for s in segments)
    description = f"{_label(sweep.base, sweep.context)}: {name} is {text}"
    changes = [numbers[run.first][0] for run in runs[1:]]
    return _finding(
        sweep, "linear", name, description,
        segments=segments, changes_at=[s["from"] for s in segments[1:]],
        evidence=_evidence(sweep, pts, _step_edges(pts, changes)),
    )


# --- evidence ----------------------------------------------------------------
#
# Every finding says how well its change points are sampled. For each change,
# the record holds the last sampled input before it and the first sampled input
# after it, with their values. ``exact`` means the two inputs are adjacent, so
# the change is pinned to one input. Otherwise ``unsampled`` counts the inputs
# between them that were never sent: the change lies within that many inputs of
# the point it is reported at.


def _step_edges(pts: Sequence[tuple[int, Any]], changes: Sequence[int]) -> list[tuple[int, int, int]]:
    """``(at, before, after)`` positions for each change: the change and its sampled neighbours."""
    xs = [x for x, _ in pts]
    previous = {xs[i]: xs[i - 1] for i in range(1, len(xs))}
    return [(change, previous[change], change) for change in changes]


def _clamp_edges(pts: Sequence[tuple[int, Any]], echo_until: Any, axis: str) -> list[tuple[int, int, int]]:
    """The clamp's start: its last echoing input and the next sampled input after it."""
    xs = [x for x, _ in pts]
    end = _ordinal(echo_until, axis)
    return [(end, end, xs[xs.index(end) + 1])]


def _change_evidence(
    pts: Sequence[tuple[int, Any]], axis: str, edges: Sequence[tuple[int, int, int]]
) -> list[dict[str, Any]]:
    """One record per change: the sampled values either side of it, and whether they are adjacent."""
    values = dict(pts)
    records = []
    for at, before, after in edges:
        width = after - before
        records.append(
            {
                "at": _show(at, axis),
                "before": {"input": _show(before, axis), "value": values[before]},
                "after": {"input": _show(after, axis), "value": values[after]},
                "exact": width == 1,
                "unsampled": width - 1,
            }
        )
    return records


def _evidence(sweep: "_Sweep", pts: Sequence[tuple[int, Any]], edges: Sequence[tuple[int, int, int]]) -> dict[str, Any]:
    """The evidence of one finding: the sweep's sample count and its change records."""
    return {"points": len(sweep.points), "changes": _change_evidence(pts, sweep.axis, edges)}


def _weaker(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """The evidence from the smaller sweep: the one that supports a merged finding least."""
    return b if b["points"] < a["points"] else a


def evidence_note(finding: dict[str, Any]) -> str:
    """A short human note on a finding's evidence, such as ``"8 points; change ±4, exact"``.

    Empty for findings without evidence (normalisation findings have their own counts).
    """
    evidence = finding.get("evidence")
    if not evidence:
        return ""
    tags = ["exact" if c["exact"] else f"±{c['unsampled']}" for c in evidence["changes"]]
    note = f"{evidence['points']} points"
    if not tags:
        return note
    label = "change" if len(tags) == 1 else "changes"
    return f"{note}; {label} {', '.join(tags)}"


# --- findings ----------------------------------------------------------------


@dataclass
class _Sweep:
    """One input swept over one set of traces, with the other inputs held as context.

    ``endpoint`` is the key findings are reported against and ``base`` the label
    they start with. ``scenario`` is ``(name, step)`` for scenario sweeps.
    """

    endpoint: str
    base: str
    param: str
    context: tuple[tuple[str, str], ...]
    axis: str
    points: list[tuple[int, Trace]]
    scenario: tuple[str, int] | None = None


def _finding(
    sweep: _Sweep,
    kind: str,
    field: str,
    description: str,
    **extra: Any,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "endpoint": sweep.endpoint,
        "param": sweep.param,
        "field": field,
        "context": dict(sweep.context),
        "kind": kind,
        "description": description,
    }
    if sweep.scenario is not None:
        finding["scenario"], finding["step"] = sweep.scenario
    finding.update(extra)
    return finding


def _sweep_findings(sweep: _Sweep, min_points: int, max_levels: int) -> list[dict[str, Any]]:
    label = _label(sweep.base, sweep.context)
    axis, param = sweep.axis, sweep.param
    points = sweep.points
    objects = [json_object(t.response) for _, t in points]
    findings: list[dict[str, Any]] = []

    shape_points = [(x, _shape(obj, t.response.status)) for (x, t), obj in zip(points, objects)]
    segments, changes = _segments(shape_points)
    if len(segments) >= 2:
        shown, shown_changes = _present(segments, changes, axis)
        parts = ", ".join(
            f"'{s['value']}' for {param} {_span(s['from'], s['to'], axis)}" for s in shown
        )
        findings.append(
            _finding(
                sweep, "shape", "shape", f"{label}: response shape is {parts}",
                segments=shown, changes_at=shown_changes,
                evidence=_evidence(sweep, shape_points, _step_edges(shape_points, changes)),
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
        if name == param and all(_ordinal(v, axis) is not None for _, v in pts):
            clamp = _clamp(pts, axis)
            if clamp is not None:
                then = clamp["then"]
                tail = f"then stays at {then}" if then is not None else "then no longer does"
                findings.append(
                    _finding(
                        sweep, "clamp", name,
                        f"{label}: {name} follows the input up to {clamp['echo_until']}, {tail}",
                        echo_until=clamp["echo_until"], then=then,
                        evidence=_evidence(sweep, pts, _clamp_edges(pts, clamp["echo_until"], axis)),
                    )
                )
            continue
        distinct = {_key(v) for _, v in pts}
        if 2 <= len(distinct) <= max_levels and len(pts) >= 2 * len(distinct):
            segments, changes = _segments(pts)
            shown, shown_changes = _present(segments, changes, axis)
            parts = ", ".join(
                f"{s['value']} for {param} {_span(s['from'], s['to'], axis)}" for s in shown
            )
            findings.append(
                _finding(
                    sweep, "step", name, f"{label}: {name} is {parts}",
                    segments=shown, changes_at=shown_changes,
                    evidence=_evidence(sweep, pts, _step_edges(pts, changes)),
                )
            )
            continue
        linear = _linear_finding(sweep, name, pts, max_levels)
        if linear is not None:
            findings.append(linear)
    return findings


def _sweeps(
    rows: Sequence[tuple[Trace, dict[str, str]]],
    param: str,
    min_points: int,
    endpoint: str,
    base: str,
    scenario: tuple[str, int] | None,
) -> list[_Sweep]:
    """Per other-input context, the first trace per distinct value of ``param``.

    ``rows`` pairs each trace with the inputs it was sent with: request
    parameters for stateless traces, the run's varied values for scenarios.
    """
    buckets: dict[tuple[tuple[str, str], ...], list[tuple[str, int, Trace]]] = {}
    for trace, values in rows:
        raw = values.get(param)
        position = _position(raw) if isinstance(raw, str) else None
        if position is None:
            continue
        context = tuple(sorted((k, v) for k, v in values.items() if k != param))
        buckets.setdefault(context, []).append((*position, trace))

    sweeps = []
    for context in sorted(buckets):
        members = buckets[context]
        axes = {axis for axis, _, _ in members}
        if len(axes) != 1:  # an input that is an int in one run and a date in another is no sweep
            continue
        first_per_value: dict[int, Trace] = {}
        for _, x, trace in sorted(members, key=lambda m: m[1]):
            first_per_value.setdefault(x, trace)
        points = sorted(first_per_value.items())
        if len(points) >= min_points:
            sweeps.append(_Sweep(endpoint, base, param, context, axes.pop(), points, scenario))
    return sweeps


def _scenario_steps(traces: Sequence[Trace]) -> dict[tuple[str, int], list[Trace]]:
    """Scenario traces grouped by (scenario family, step index); variants count with their template."""
    steps: dict[tuple[str, int], list[Trace]] = {}
    for trace in traces:
        if trace.scenario is not None:
            key = (scenario_family(str(trace.scenario["name"])), int(trace.scenario["step"]))
            steps.setdefault(key, []).append(trace)
    return steps


def _order(finding: dict[str, Any]) -> tuple[Any, ...]:
    return (
        finding["endpoint"],
        finding["param"],
        finding.get("scenario", ""),
        finding.get("step", -1),
        json.dumps(finding["context"], sort_keys=True),
        finding["field"],
    )


def _payload(finding: dict[str, Any]) -> Any:
    if "segments" in finding:
        return finding["segments"]
    return [finding["echo_until"], finding["then"]]


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one finding per identical result across contexts, counting the contexts.

    Scenario findings keep their scenario and step in the signature, so the same
    endpoint at two steps of one scenario stays two findings.
    """
    kept: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in sorted(findings, key=_order):
        signature = (
            finding["endpoint"],
            finding["param"],
            finding["field"],
            finding["kind"],
            json.dumps(_payload(finding), sort_keys=True),
            finding.get("scenario"),
            finding.get("step"),
        )
        if signature in kept:
            kept[signature]["contexts"] += 1
            kept[signature]["evidence"] = _weaker(kept[signature]["evidence"], finding["evidence"])
        else:
            finding["contexts"] = 1
            kept[signature] = finding
    return _merge_steps(list(kept.values()))


def _merge_steps(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report a scenario result once when several steps of one run show it.

    A double return, for example, answers the same fee at step 2 and step 3.
    The kept finding gets ``steps`` (every step, ascending) and a label that
    names them all; ``step`` stays the first one.
    """
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("scenario") is None:
            order.append(finding)
            continue
        signature = (
            finding["scenario"],
            finding["endpoint"],
            finding["param"],
            finding["field"],
            finding["kind"],
            json.dumps(_payload(finding), sort_keys=True),
        )
        first = merged.get(signature)
        if first is None:
            finding["steps"] = [finding["step"]]
            merged[signature] = finding
            order.append(finding)
        else:
            first["steps"].append(finding["step"])
            first["evidence"] = _weaker(first["evidence"], finding["evidence"])
    for finding in merged.values():
        steps = sorted(finding["steps"])
        finding["steps"] = steps
        if len(steps) > 1:
            old = f"{finding['scenario']} step {steps[0]} ("
            new = f"{finding['scenario']} steps {', '.join(str(s) for s in steps)} ("
            finding["description"] = finding["description"].replace(old, new, 1)
    return order


def detect_boundaries(
    traces: Sequence[Trace], min_points: int = 8, max_levels: int = 6
) -> list[dict[str, Any]]:
    """Find shape changes, step functions, clamps and linear segments along input sweeps.

    Stateless traces are analysed per endpoint and request parameter; scenario
    traces per (scenario, step) and scenario variable, and their findings carry
    ``scenario`` and ``step`` keys. Output is sorted by (endpoint, param, scenario,
    step, context, field) and deduplicated across contexts with a ``contexts`` count.
    """
    findings: list[dict[str, Any]] = []
    stateless = [t for t in traces if t.scenario is None]
    for endpoint, group in sorted(group_by_endpoint(stateless).items()):
        rows = [(t, t.request.params) for t in group]
        names = sorted({name for t in group for name in t.request.params})
        for param in names:
            for sweep in _sweeps(rows, param, min_points, endpoint, endpoint, None):
                findings.extend(_sweep_findings(sweep, min_points, max_levels))

    for (name, step), group in sorted(_scenario_steps(traces).items()):
        endpoint = endpoint_key(group[0])
        base = f"{name} step {step} ({endpoint})"
        rows = [(t, dict((t.scenario or {}).get("vars") or {})) for t in group]
        names = sorted({var for _, values in rows for var in values})
        for var in names:
            for sweep in _sweeps(rows, var, min_points, endpoint, base, (name, step)):
                findings.extend(_sweep_findings(sweep, min_points, max_levels))
    return _dedupe(findings)
