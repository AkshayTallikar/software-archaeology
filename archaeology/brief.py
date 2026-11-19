"""Turn an inferred spec plus recorded traces into a rebuild brief.

The brief is the hand-off to whoever rebuilds the system, typically coding
agents. It is built only from evidence: the inferred spec and real recorded
exchanges. It never quotes the legacy source or the legacy documentation,
except to say where the documentation is wrong.

Every section is derived in the order the inputs arrive, so the same spec and
traces always give the same brief.
"""

from __future__ import annotations

import json
import re
import string
from collections import OrderedDict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .infer.catalog import endpoint_key, flatten_json, json_object, value_type
from .trace import Trace

MAX_SCENARIO_RUNS = 10

_ID_PREFIX = re.compile(r"([A-Za-z]{1,4}[-_])?(.*)", re.DOTALL)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DIGITS = frozenset(string.digits)
_LOWER_HEX = frozenset("0123456789abcdef")
_UPPER_HEX = frozenset("0123456789ABCDEF")
_LOWER_ALNUM = frozenset(string.digits + string.ascii_lowercase)
_ALNUM = frozenset(string.digits + string.ascii_letters)


def _shape(trace: Trace) -> str:
    body = trace.response.json()
    if isinstance(body, dict):
        return f"{trace.response.status} json:{','.join(sorted(body))}"
    return f"{trace.response.status} {trace.response.content_type or 'text'}"


def _request_line(trace: Trace) -> str:
    req = trace.request
    line = f"{req.method} {req.path}"
    if req.query_string:
        line += f"?{req.query_string}"
    if req.body:
        line += f"\n  body ({req.content_type or 'raw'}): {req.body}"
    return line


def _render_exchange(trace: Trace) -> str:
    resp = trace.response
    body = resp.json()
    shown = json.dumps(body, indent=None, sort_keys=True) if body is not None else repr(resp.body)
    return (
        f"```\n{_request_line(trace)}\n"
        f"-> {resp.status} {resp.content_type}\n{shown}\n```"
    )


def select_examples(
    traces: Iterable[Trace],
    boundaries: list[dict[str, Any]] | None = None,
    per_shape: int = 3,
) -> "OrderedDict[str, list[Trace]]":
    """Pick representative exchanges per endpoint.

    For each endpoint: up to ``per_shape`` exchanges for every distinct
    response shape (preferring distinct parameter combinations), plus the
    exchanges on either side of every inferred boundary.
    """
    traces = list(traces)
    by_endpoint: "OrderedDict[str, list[Trace]]" = OrderedDict()
    seen_ids: set[str] = set()
    per_shape_count: dict[tuple[str, str], int] = {}
    seen_params: set[tuple[str, tuple]] = set()

    def add(trace: Trace) -> None:
        if trace.id in seen_ids:
            return
        seen_ids.add(trace.id)
        by_endpoint.setdefault(endpoint_key(trace), []).append(trace)

    for trace in traces:
        key = endpoint_key(trace)
        shape_key = (key, _shape(trace))
        param_sig = (key, tuple(sorted(trace.request.params.items())))
        if per_shape_count.get(shape_key, 0) >= per_shape or param_sig in seen_params:
            continue
        per_shape_count[shape_key] = per_shape_count.get(shape_key, 0) + 1
        seen_params.add(param_sig)
        add(trace)

    for finding in boundaries or []:
        points = set()
        for x in finding.get("changes_at") or []:
            points.update({x - 1, x})
        if finding.get("kind") == "clamp" and finding.get("echo_until") is not None:
            points.update({finding["echo_until"], finding["echo_until"] + 1})
        if not points:
            continue
        context = {k: str(v) for k, v in (finding.get("context") or {}).items()}
        param = finding["param"]
        for trace in traces:
            if endpoint_key(trace) != finding["endpoint"]:
                continue
            params = dict(trace.request.params)
            value = params.pop(param, None)
            if value is None or params != context:
                continue
            try:
                if int(value) in points:
                    add(trace)
            except ValueError:
                continue

    for key in by_endpoint:
        by_endpoint[key].sort(key=lambda t: t.id)
    return by_endpoint


# --- identifier formats ------------------------------------------------------


def _char_class(chars: set[str]) -> str:
    """The narrowest common character class name for a set of characters."""
    if chars <= _DIGITS:
        return "digits"
    if chars <= _LOWER_HEX:
        return "lowercase hex characters"
    if chars <= _UPPER_HEX:
        return "uppercase hex characters"
    if chars <= _LOWER_ALNUM:
        return "lowercase letters or digits"
    if chars <= _ALNUM:
        return "letters or digits"
    return "characters"


def describe_id_format(values: Sequence[str]) -> str:
    """A short pattern for generated identifiers: prefix, length and character class.

    ``["m_3fa9c2d1", "m_77c0e1aa"]`` gives ``"`m_` followed by 8 lowercase hex characters"``.
    """
    distinct = [v for v in dict.fromkeys(values) if v]
    if not distinct:
        return "no values observed"
    if all(_UUID.fullmatch(v) for v in distinct):
        return "a UUID (8-4-4-4-12 hexadecimal characters)"
    prefixes = {_ID_PREFIX.fullmatch(v).group(1) or "" for v in distinct}
    prefix = prefixes.pop() if len(prefixes) == 1 else ""
    bodies = [v[len(prefix):] for v in distinct]
    lengths = {len(b) for b in bodies}
    if len(lengths) == 1:
        span = str(lengths.pop())
    else:
        span = f"{min(lengths)} to {max(lengths)}"
    chars = {c for b in bodies for c in b}
    shape = f"{span} {_char_class(chars)}"
    if prefix:
        return f"`{prefix}` followed by {shape}"
    return shape


def identifier_formats(flows: Sequence[dict[str, Any]], traces: Iterable[Trace]) -> dict[tuple[str, str], tuple[str, int]]:
    """``(source endpoint, field) -> (format description, distinct values seen)`` for each flow source."""
    values: dict[tuple[str, str], list[str]] = {(f["from"], f["field"]): [] for f in flows}
    for trace in traces:
        obj = json_object(trace.response)
        if obj is None:
            continue
        key_name = endpoint_key(trace)
        for path, value in flatten_json(obj).items():
            bucket = values.get((key_name, path))
            if bucket is not None and isinstance(value, str):
                bucket.append(value)
    return {key: (describe_id_format(vals), len(set(vals))) for key, vals in values.items()}


# --- scenario runs -----------------------------------------------------------


def _order_key(raw: str) -> tuple[int, Decimal, str]:
    """Sort key for a varied value: dates and numbers by value, anything else as text."""
    kind = value_type(raw)
    if kind == "date":
        return (0, Decimal(date.fromisoformat(raw).toordinal()), "")
    if kind in ("int", "float"):
        return (0, Decimal(raw), "")
    return (1, Decimal(0), raw)


def _pick_runs(
    runs: dict[int, list[Trace]],
    findings: Sequence[dict[str, Any]],
) -> list[int]:
    """Run numbers to show: first and last, then the runs either side of each change point.

    Stops at :data:`MAX_SCENARIO_RUNS`, keeping the higher-priority picks.
    """
    run_numbers = sorted(runs)
    picks: list[int] = [run_numbers[0], run_numbers[-1]]
    for finding in findings:
        param = finding.get("param")
        ordered = []
        for run in run_numbers:
            raw = (runs[run][0].scenario or {}).get("vars", {}).get(param)
            if raw is not None:
                ordered.append((_order_key(str(raw)), run))
        ordered.sort()
        for change in finding.get("changes_at") or []:
            change_key = _order_key(str(change))
            below = [run for key, run in ordered if key < change_key]
            above = [run for key, run in ordered if key >= change_key]
            if below:
                picks.append(below[-1])
            if above:
                picks.append(above[0])
    unique = list(dict.fromkeys(picks))[:MAX_SCENARIO_RUNS]
    return sorted(unique)


def _scenario_section(spec: dict[str, Any], traces: Sequence[Trace]) -> list[str]:
    lines = [
        "## Recorded scenario runs",
        "",
        "Complete multi-step runs as recorded, every step in order. Identifiers in these runs are the "
        "recorded values; a rebuild issues its own, and replay maps them. Each scenario shows a few runs "
        "chosen to cover its first and last run and the runs either side of each change point.",
        "",
    ]
    scenario_traces = [t for t in traces if t.scenario is not None]
    if not scenario_traces:
        return lines + ["_No scenarios recorded._", ""]

    by_scenario: "OrderedDict[str, dict[int, list[Trace]]]" = OrderedDict()
    for trace in scenario_traces:
        name = str(trace.scenario["name"])
        by_scenario.setdefault(name, {}).setdefault(int(trace.scenario["run"]), []).append(trace)

    boundaries = spec.get("boundaries", [])
    for name, runs in by_scenario.items():
        for run_steps in runs.values():
            run_steps.sort(key=lambda t: int(t.scenario["step"]))
        findings = [f for f in boundaries if f.get("scenario") == name]
        lines += [f"### Scenario `{name}`", ""]
        for run in _pick_runs(runs, findings):
            steps = runs[run]
            varied = (steps[0].scenario or {}).get("vars") or {}
            varied_text = ", ".join(f"{k}={v}" for k, v in sorted(varied.items())) or "no varied values"
            lines += [f"#### Run {run} ({varied_text})", ""]
            for trace in steps:
                lines.append(f"Step {trace.scenario['step']}:")
                lines.append("")
                lines.append(_render_exchange(trace))
                lines.append("")
    return lines


# --- piecewise-linear tables --------------------------------------------------


def _linear_section(boundaries: Sequence[dict[str, Any]]) -> list[str]:
    linear = [b for b in boundaries if b.get("kind") == "linear" and b.get("segments")]
    if not linear:
        return []
    lines = ["### Piecewise-linear rules", ""]
    for finding in linear:
        where = f" in scenario `{finding['scenario']}`" if finding.get("scenario") else ""
        lines += [f"#### `{finding['field']}` of `{finding['endpoint']}`{where}", "", finding["description"], ""]
        is_date = value_type(str(finding["segments"][0]["from"])) == "date"
        unit = "day" if is_date else finding["param"]
        lines += [
            f"| {finding['param']} from | {finding['param']} to | start value | end value | slope per {unit} |",
            "| --- | --- | --- | --- | --- |",
        ]
        for seg in finding["segments"]:
            lines.append(f"| {seg['from']} | {seg['to']} | {seg['start']} | {seg['end']} | {seg['slope']} |")
        lines.append("")
    return lines


# --- brief -------------------------------------------------------------------


def generate_brief(
    spec: dict[str, Any],
    traces: Iterable[Trace],
    system_name: str = "the legacy system",
    per_shape: int = 3,
) -> str:
    """Render the rebuild brief as markdown."""
    traces = list(traces)
    boundaries = spec.get("boundaries", [])
    contradicted = [c for c in spec.get("claims", []) if c.get("verdict") == "CONTRADICTED"]
    hidden = [b["description"] for b in boundaries] + [n["description"] for n in spec.get("normalization", [])]

    lines: list[str] = [
        f"# Rebuild brief: {system_name}",
        "",
        f"Derived from {spec.get('trace_count', len(traces))} recorded exchanges with the running system. "
        "This brief is the specification. Where it disagrees with any documentation you may have seen, "
        "this brief is right: it describes what the system actually does, and users depend on that.",
        "",
        "## Ground rules",
        "",
        "1. Reproduce observed behaviour exactly, including behaviour that looks like a bug. "
        "Status codes, content types, error bodies and JSON field names and types must match.",
        "2. Fields listed under *Volatile fields* must be present, but their values may differ.",
        "3. Your rebuild is done when replaying every recorded exchange against it reports 100% equivalence.",
        "4. The system is stateful: a value one response generates is sent back by later requests. "
        "Replay does not compare exact identifier values; it maps them through an identity map, so only "
        "their format has to match (see *Identifier flows*).",
        "",
        "## Rules the evidence shows",
        "",
        "Every hidden behaviour and every documented claim that the traffic contradicts. Details and "
        "examples follow below.",
        "",
    ]
    lines += [f"- {h}" for h in hidden]
    lines += [
        f"- Documented claim contradicted: \"{c['text']}\". Reality: {'; '.join(c.get('mismatches', [])) or 'see details'}."
        for c in contradicted
    ]
    if not hidden and not contradicted:
        lines.append("- None detected.")
    lines.append("")

    lines += ["## Endpoints", ""]
    for ep in spec.get("endpoints", []):
        lines.append(f"### `{ep['endpoint']}`  ({ep['count']} recorded exchanges)")
        lines.append("")
        params = ep.get("params") or {}
        if params:
            lines.append("| Parameter | Required | Observed types | Example values |")
            lines.append("| --- | --- | --- | --- |")
            for name, info in params.items():
                examples = ", ".join(f"`{v!r}`" for v in info.get("examples", []))
                lines.append(
                    f"| `{name}` | {'yes' if info.get('required') else 'no'} | "
                    f"{', '.join(info.get('types', []))} | {examples} |"
                )
            lines.append("")
        lines.append("Response shapes:")
        lines.append("")
        for shape in ep.get("responses", []):
            if shape.get("json_keys") is not None:
                fields = ", ".join(
                    f"`{k}`: {'/'.join(shape['field_types'].get(k, []))}" for k in shape["json_keys"]
                )
                lines.append(f"- {shape['status']} `{shape['content_type']}` JSON object with {fields} ({shape['count']}x)")
            else:
                examples = "; ".join(repr(t) for t in shape.get("text_examples", []))
                lines.append(f"- {shape['status']} `{shape['content_type']}` text, e.g. {examples} ({shape['count']}x)")
        lines.append("")

    flows = spec.get("flows", [])
    lines += ["## Identifier flows", ""]
    if flows:
        lines += [
            "Clients chain calls: an identifier returned by one request is sent in later requests. "
            "Exact identifier values are not compared by replay, only their format.",
            "",
        ]
        lines += [f"- {f['description']}" for f in flows]
        lines += ["", "Identifier formats observed at each source:", ""]
        formats = identifier_formats(flows, traces)
        for (source, field), (description, distinct) in formats.items():
            lines.append(f"- `{source}` field `{field}`: {description} ({distinct} distinct values)")
    else:
        lines.append("_None detected._")
    lines.append("")

    lines += ["## Hidden behaviour (inferred from traffic)", ""]
    lines += [f"- {h}" for h in hidden] or ["- None detected."]
    lines.append("")
    linear = _linear_section(boundaries)
    if linear:
        lines += ["## Rule tables", ""] + linear

    if contradicted:
        lines += ["## Where the old documentation is wrong", ""]
        for claim in contradicted:
            evidence = "; ".join(claim.get("mismatches", []))
            lines.append(f"- Docs claim: \"{claim['text']}\". Reality: {evidence}.")
        lines.append("")

    lines += ["## Volatile fields", ""]
    volatile = spec.get("volatile_fields", {})
    if volatile:
        for key, fields in volatile.items():
            for f in fields:
                lines.append(f"- `{key}` → `{f['path']}` ({f['reason']})")
    else:
        lines.append("- None detected.")
    lines.append("")

    lines += _scenario_section(spec, traces)

    # Stateless examples come from stateless traces only; scenario steps are shown in full above.
    # An endpoint seen only inside scenarios still gets examples, from its scenario steps.
    stateless = [t for t in traces if t.scenario is None]
    stateless_boundaries = [b for b in boundaries if "scenario" not in b]
    examples = select_examples(stateless, stateless_boundaries, per_shape=per_shape)
    stateless_endpoints = {endpoint_key(t) for t in stateless}
    scenario_only = [
        t for t in traces if t.scenario is not None and endpoint_key(t) not in stateless_endpoints
    ]
    for key, chosen in select_examples(scenario_only, None, per_shape=per_shape).items():
        examples.setdefault(key, []).extend(chosen)

    lines += [
        "## Example exchanges",
        "",
        "Real recorded exchanges, chosen to cover every response shape and both sides of every boundary. "
        "Endpoints seen only inside scenarios are shown from their scenario steps.",
        "",
    ]
    for key, chosen in examples.items():
        lines.append(f"### `{key}`")
        lines.append("")
        for trace in chosen:
            lines.append(_render_exchange(trace))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
