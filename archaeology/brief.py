"""Turn an inferred spec plus recorded traces into a rebuild brief.

The brief is the hand-off to whoever rebuilds the system, typically coding
agents. It is built only from evidence: the inferred spec and real recorded
exchanges. It never quotes the legacy source or the legacy documentation,
except to say where the documentation is wrong.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Iterable

from .infer.catalog import endpoint_key
from .trace import Trace


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


def generate_brief(
    spec: dict[str, Any],
    traces: Iterable[Trace],
    system_name: str = "the legacy system",
    per_shape: int = 3,
) -> str:
    """Render the rebuild brief as markdown."""
    traces = list(traces)
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
        "",
        "## Endpoints",
        "",
    ]

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

    hidden = [b["description"] for b in spec.get("boundaries", [])]
    hidden += [n["description"] for n in spec.get("normalization", [])]
    lines += ["## Hidden behaviour (inferred from traffic)", ""]
    lines += [f"- {h}" for h in hidden] or ["- None detected."]
    lines.append("")

    contradicted = [c for c in spec.get("claims", []) if c.get("verdict") == "CONTRADICTED"]
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

    lines += [
        "## Example exchanges",
        "",
        "Real recorded exchanges, chosen to cover every response shape and both sides of every boundary.",
        "",
    ]
    examples = select_examples(traces, spec.get("boundaries"), per_shape=per_shape)
    for key, chosen in examples.items():
        lines.append(f"### `{key}`")
        lines.append("")
        for trace in chosen:
            lines.append(_render_exchange(trace))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
