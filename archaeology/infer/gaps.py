"""Evidence gaps: what the recording never covered, and the workload addition that would cover it.

Five kinds of gap come out of a recording:

* ``inexact_change``: a change point whose sampled neighbours are not adjacent,
  so the exact edge was never seen. A bracket seen in several contexts is
  reported once.
* ``few_values``: a request parameter or scenario variable seen with exactly two
  distinct values. A variable with one value is a fixed base input that no
  sweep meant to vary, so it is not reported; three or more values count as
  varied.
* ``single_status``: an endpoint seen at least :data:`MIN_STATUS_REQUESTS` times
  that only ever returned one HTTP status, so its other outcomes are unknown.
* ``failed_capture``: a scenario step whose request still holds an unbound
  ``{{placeholder}}``. The placeholder is either a varied variable that a run
  left out (its null value was sent literally) or a captured value that an
  earlier step never bound. Such requests are excluded from ``single_status``.
* ``unobserved_claim``: a documented claim that no recorded exchange tests.

Each gap is ``{kind, where, detail, suggestion, data}``; ``suggestion`` is one
line of workload to add or fix, in plain words. ``data`` holds the same facts in
machine-readable form (endpoint, method, path, parameter or scenario variable,
bracketing inputs, observed values or statuses, and for inexact changes the
contexts whose sampled inputs bracket the change), so a tool can act on a gap
without parsing its text. The text fields are unchanged by ``data``.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from datetime import date

from ..scenarios import scenario_family
from ..trace import FORM_CONTENT_TYPE, Trace
from .catalog import endpoint_key, path_template, value_type

FEW_VALUES = 3  # two distinct values is thin; three or more is varied
MIN_STATUS_REQUESTS = 2  # one request cannot show a status change, so it is not reported
WRITE_METHODS = ("POST", "PUT", "PATCH")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def detect_evidence_gaps(
    traces: Sequence[Trace],
    boundaries: Sequence[dict[str, Any]],
    claims: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """All evidence gaps in the recording, grouped by kind in a fixed order."""
    traces = list(traces)
    return (
        _inexact_changes(boundaries, traces)
        + _few_values(traces)
        + _single_statuses(traces)
        + _failed_captures(traces)
        + _unobserved_claims(claims)
    )


def _gap(kind: str, where: str, detail: str, suggestion: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"kind": kind, "where": where, "detail": detail, "suggestion": suggestion, "data": data or {}}


def _position(raw: str) -> tuple[str, int] | None:
    """``(axis, position)`` for a raw integer or ISO date input, else None."""
    kind = value_type(raw)
    if kind == "int":
        return "int", int(raw)
    if kind == "date":
        return "date", date.fromisoformat(raw).toordinal()
    return None


def _location(trace: Trace) -> str:
    """Where a stateless request's parameters travel: ``form`` for form bodies, else ``query``."""
    return "form" if trace.request.content_type == FORM_CONTENT_TYPE and trace.request.body else "query"


def _bracket_contexts(rows: Sequence[tuple[Trace, dict[str, str]]], param: str, lo: str, hi: str) -> list[dict[str, str]]:
    """The contexts in which ``lo`` and ``hi`` are both sampled with no sampled input between them.

    ``rows`` pairs each trace with the inputs it was sent with (request parameters,
    or a scenario run's varied values). A context is the other inputs.
    """
    bracket = _position(lo)
    if bracket is None or _position(hi) is None or _position(hi)[0] != bracket[0]:
        return []
    axis, low, high = bracket[0], bracket[1], _position(hi)[1]
    groups: dict[tuple[tuple[str, str], ...], set[int]] = defaultdict(set)
    for _, values in rows:
        raw = values.get(param)
        position = _position(raw) if isinstance(raw, str) else None
        if position is None or position[0] != axis:
            continue
        context = tuple(sorted((k, v) for k, v in values.items() if k != param))
        groups[context].add(position[1])
    return [
        dict(context)
        for context, positions in sorted(groups.items())
        if low in positions and high in positions and not any(low < p < high for p in positions)
    ]


def _endpoint_rows(traces: Sequence[Trace], endpoint: str) -> list[tuple[Trace, dict[str, str]]]:
    return [(t, t.request.params) for t in traces if t.scenario is None and endpoint_key(t) == endpoint]


def _scenario_rows(traces: Sequence[Trace], name: str, step: int) -> list[tuple[Trace, dict[str, str]]]:
    return [
        (t, {k: str(v) for k, v in (t.scenario.get("vars") or {}).items()})
        for t in traces
        if t.scenario is not None and scenario_family(str(t.scenario["name"])) == name
        and int(t.scenario["step"]) == step
    ]


def _or_join(items: Sequence[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " or " + items[-1]


def _context_text(context: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(context.items()))


def _finding_base(finding: dict[str, Any]) -> str:
    if finding.get("scenario") is None:
        return str(finding["endpoint"])
    return f"scenario {finding['scenario']} step {finding['step']} ({finding['endpoint']})"


# --- inexact change points ---------------------------------------------------


def _inexact_data(finding: dict[str, Any], traces: Sequence[Trace], lo: str, hi: str) -> dict[str, Any]:
    """The machine-readable facts of one inexact change: its bracket, and every context that shares it."""
    param = finding["param"]
    if finding.get("scenario") is None:
        rows = _endpoint_rows(traces, finding["endpoint"])
        sample = next(t for t, v in rows if str(v.get(param)) in (lo, hi))
        data: dict[str, Any] = {
            "endpoint": finding["endpoint"],
            "method": sample.request.method,
            "path": sample.request.path,
            "location": _location(sample),
            "scenario": None,
            "step": None,
        }
    else:
        rows = _scenario_rows(traces, finding["scenario"], int(finding["step"]))
        data = {"endpoint": finding["endpoint"], "scenario": finding["scenario"], "step": int(finding["step"])}
    data.update({
        "param": param,
        "field": finding["field"],
        "axis": "date" if value_type(lo) == "date" else "int",
        "lo": lo,
        "hi": hi,
        "contexts": _bracket_contexts(rows, param, lo, hi),
    })
    return data


def _inexact_changes(boundaries: Sequence[dict[str, Any]], traces: Sequence[Trace]) -> list[dict[str, Any]]:
    """One gap per inexact change; a bracket that several contexts share is reported once."""
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for finding in boundaries:
        context = {k: str(v) for k, v in (finding.get("context") or {}).items()}
        for change in (finding.get("evidence") or {}).get("changes", []):
            if change["exact"]:
                continue
            lo, hi = str(change["before"]["input"]), str(change["after"]["input"])
            key = (_finding_base(finding), finding["param"], finding["field"], lo, hi)
            contexts = finding.get("contexts", 1)  # a finding already merged across contexts counts them all
            if key not in grouped:
                grouped[key] = {"finding": finding, "context": context, "unsampled": change["unsampled"],
                                "contexts": contexts}
            else:
                grouped[key]["contexts"] += contexts
    gaps = []
    for (base, param, field, lo, hi), entry in grouped.items():
        finding, context = entry["finding"], entry["context"]
        ctx = _context_text(context)
        others = entry["contexts"] - 1
        more = f"; {others} more context{'s' if others > 1 else ''}" if others else ""
        where = f"{base} ({ctx}{more}), {param}" if ctx else f"{base}, {param}"
        if finding.get("scenario") is None:
            on = f" on {finding['endpoint']}" + (f" ({ctx})" if ctx else "")
            fix = f"add a sweep of {param} from {lo} to {hi}{on}"
        else:
            fix = f"add runs of scenario {finding['scenario']} with {param} from {lo} to {hi}"
        gaps.append(
            _gap(
                "inexact_change",
                where,
                f"{field} changes somewhere between sampled {lo} and {hi}; "
                f"{entry['unsampled']} inputs between them were never sent",
                fix,
                _inexact_data(finding, traces, lo, hi),
            )
        )
    return gaps


# --- few distinct values -----------------------------------------------------


def _input_values(
    traces: Sequence[Trace],
) -> tuple[dict[tuple[str, str, str], set[str]], Counter[tuple[str, str, str]]]:
    """Distinct values and request counts per input, keyed by (owner kind, owner, name)."""
    values: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    counts: Counter[tuple[str, str, str]] = Counter()
    for trace in traces:
        if trace.scenario is None:
            for name, value in trace.request.params.items():
                key = ("request", endpoint_key(trace), name)
                values[key].add(value)
                counts[key] += 1
        else:
            for name, value in (trace.scenario.get("vars") or {}).items():
                key = ("scenario", scenario_family(str(trace.scenario["name"])), name)
                values[key].add(str(value))
                counts[key] += 1
    return values, counts


def _few_values(traces: Sequence[Trace]) -> list[dict[str, Any]]:
    values, counts = _input_values(traces)
    gaps = []
    for key in sorted(values):
        seen = sorted(values[key])
        if not 2 <= len(seen) < FEW_VALUES:
            continue
        owner_kind, owner, name = key
        listed = ", ".join(f"'{v}'" for v in seen)
        if owner_kind == "request":
            where = f"{owner} parameter {name}"
            fix = f"add {owner} requests that try {name} with a third value, such as one the documentation names"
            sample = next(t for t in traces if t.scenario is None and endpoint_key(t) == owner
                          and name in t.request.params)
            data = {"kind": "request", "endpoint": owner, "method": sample.request.method,
                    "path": sample.request.path, "location": _location(sample)}
        else:
            where = f"scenario {owner} variable {name}"
            fix = f"add runs of scenario {owner} with {name} set to a third value"
            data = {"kind": "scenario", "scenario": owner}
        data.update({"param": name, "seen": seen})
        gaps.append(_gap("few_values", where, f"only {listed} seen, across {counts[key]} requests", fix, data))
    return gaps


# --- single statuses ---------------------------------------------------------


def _placeholders(trace: Trace) -> list[str]:
    """Unbound ``{{name}}`` placeholders in a recorded request's path, parameters and body."""
    request = trace.request
    text = " ".join([request.path, request.body, *request.params.keys(), *request.params.values()])
    return sorted(set(_PLACEHOLDER.findall(text)))


def _error_probes(endpoint: str) -> str:
    """Plain words for the requests most likely to show an endpoint's other statuses."""
    method, _, path = endpoint.partition(" ")
    probes = []
    if "{id}" in path:
        probes.append("an unknown id")
    if method in WRITE_METHODS:
        probes.append("a missing field")
    probes.append("an invalid value")
    return f"{endpoint} requests with {_or_join(probes)}"


def _single_statuses(traces: Sequence[Trace]) -> list[dict[str, Any]]:
    by_endpoint: dict[str, list[Trace]] = defaultdict(list)
    for trace in traces:
        if not _placeholders(trace):
            by_endpoint[endpoint_key(trace)].append(trace)
    gaps = []
    for endpoint in sorted(by_endpoint):
        group = by_endpoint[endpoint]
        statuses = {t.response.status for t in group}
        if len(statuses) != 1 or len(group) < MIN_STATUS_REQUESTS:
            continue
        status = next(iter(statuses))
        method, _, _ = endpoint.partition(" ")
        sample = group[0]
        gaps.append(
            _gap(
                "single_status",
                endpoint,
                f"only HTTP {status} seen, across {len(group)} requests",
                f"add {_error_probes(endpoint)} to see its other statuses",
                {
                    "endpoint": endpoint,
                    "method": method,
                    "path": sample.request.path,
                    "template": path_template(sample.request.path),
                    "location": _location(sample),
                    "statuses": [status],
                    "requests": len(group),
                },
            )
        )
    return gaps


# --- failed captures ---------------------------------------------------------


def _braces(names: Sequence[str]) -> str:
    return ", ".join("{{" + name + "}}" for name in names)


def _failed_captures(traces: Sequence[Trace]) -> list[dict[str, Any]]:
    runs: Counter[tuple[str, int]] = Counter()
    failed: dict[tuple[str, int], dict[str, Any]] = {}
    varied: dict[str, set[str]] = defaultdict(set)  # scenario name -> variables some run varies
    for trace in traces:
        if trace.scenario is None:
            continue
        name, step = scenario_family(str(trace.scenario["name"])), int(trace.scenario["step"])
        varied[name].update((trace.scenario.get("vars") or {}).keys())
        runs[(name, step)] += 1
        names = _placeholders(trace)
        if names:
            entry = failed.setdefault((name, step), {"runs": 0, "names": set(), "endpoint": endpoint_key(trace)})
            entry["runs"] += 1
            entry["names"].update(names)
    gaps = []
    for key in sorted(failed):
        name, step = key
        entry = failed[key]
        unbound = sorted(entry["names"])
        from_varied = [n for n in unbound if n in varied[name]]
        from_capture = [n for n in unbound if n not in varied[name]]
        details, fixes = [], []
        if from_varied:
            details.append(
                f"{entry['runs']} of {runs[key]} runs sent {_braces(from_varied)} unbound, "
                f"though the scenario varies {', '.join(from_varied)}"
            )
            fixes.append(f"make a null {', '.join(from_varied)} omit the field instead of sending the placeholder")
        if from_capture:
            details.append(
                f"{entry['runs']} of {runs[key]} runs sent {_braces(from_capture)} unbound, "
                "because an earlier step did not capture it"
            )
            fixes.append(f"make the earlier step that captures {_braces(from_capture)} succeed in every run")
        gaps.append(
            _gap(
                "failed_capture",
                f"scenario {name} step {step} ({entry['endpoint']})",
                "; ".join(details),
                f"in scenario {name}, " + "; ".join(fixes),
                {
                    "scenario": name,
                    "step": step,
                    "endpoint": entry["endpoint"],
                    "unbound": unbound,
                    "from_varied": from_varied,
                    "from_capture": from_capture,
                },
            )
        )
    return gaps


# --- unobserved claims -------------------------------------------------------


def _claim_request_text(request: dict[str, Any]) -> str:
    if "scenario" in request:
        variables = request.get("vars") or {}
        varied = ", ".join(f"{k}={v}" for k, v in sorted(variables.items()))
        suffix = f" with {varied}" if varied else ""
        return f"a run of scenario {request['scenario']} that reaches step {request['step']}{suffix}"
    params = {**(request.get("query") or {}), **(request.get("form") or {})}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    path = f"{str(request.get('method', 'GET')).upper()} {request.get('path', '')}"
    return f"a request {path}{'?' + query if query else ''}"


def _unobserved_claims(claims: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _gap(
            "unobserved_claim",
            f"claim {claim['id']}",
            f"no recorded exchange tests \"{claim['text']}\"",
            f"add {_claim_request_text(claim['request'])} to the workload",
            {"claim": claim["id"], "request": claim["request"]},
        )
        for claim in claims
        if claim.get("verdict") == "UNOBSERVED"
    ]
