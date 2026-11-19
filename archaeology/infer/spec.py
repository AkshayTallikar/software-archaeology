"""Assemble inferred behaviour into a spec, a readable report, and JSON on disk.

A spec is a JSON object with these top-level keys:

* ``version``, ``trace_count``, ``endpoints``, ``volatile_fields``, ``flows``,
  ``normalization``: the inferred behaviour, as before.
* ``boundaries``: findings from input sweeps. Each carries ``evidence``:
  ``points`` (samples in its sweep or context) and ``changes``, one record per
  change point with the last sampled ``before`` and first sampled ``after``
  input and value, ``exact`` (the two inputs are adjacent) and ``unsampled``
  (inputs between them that were never sent). Findings merged across contexts
  or steps keep the evidence of their smallest sweep.
* ``claims``: each result has ``support``, ``matching`` and ``contradicting``
  trace counts, and the claim's ``request``.
* ``evidence_gaps``: what the recording never covered, each ``{kind, where,
  detail, suggestion}``. See :mod:`archaeology.infer.gaps`.

Specs written before evidence existed lack ``evidence``, ``support`` and
``evidence_gaps``; readers treat those keys as absent.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..trace import Request, Trace
from .boundaries import detect_boundaries
from .catalog import build_catalog, endpoint_key_for
from .claims import check_claims
from .boundaries import evidence_note
from .flows import detect_flows
from .gaps import detect_evidence_gaps
from .normalization import detect_normalization
from .volatility import detect_volatile_fields, volatile_paths

SPEC_VERSION = 2
_VERDICT_ORDER = {"CONTRADICTED": 0, "CONFIRMED": 1, "UNOBSERVED": 2}


def infer_spec(traces: Sequence[Trace], claims: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Infer the full behavioural spec from recorded traces and optional claims."""
    traces = list(traces)
    volatile = detect_volatile_fields(traces)
    boundaries = detect_boundaries(traces)
    checked = check_claims(claims, traces) if claims else []
    return {
        "version": SPEC_VERSION,
        "trace_count": len(traces),
        "endpoints": build_catalog(traces),
        "volatile_fields": volatile,
        "boundaries": boundaries,
        "flows": detect_flows(traces),
        "normalization": detect_normalization(traces, volatile),
        "claims": checked,
        "evidence_gaps": detect_evidence_gaps(traces, boundaries, checked),
    }


def volatile_paths_for(spec: dict[str, Any], request: Request) -> set[str]:
    """Volatile field paths to ignore when comparing responses to this request's endpoint."""
    return volatile_paths(spec.get("volatile_fields", {}), endpoint_key_for(request.method, request.path))


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _evidence(claim: dict[str, Any]) -> str:
    if claim["mismatches"]:
        return "; ".join(claim["mismatches"])
    if claim["observed"]:
        return f"observed status {claim['observed']['status']}"
    return "no matching trace"


def _note_suffix(finding: dict[str, Any]) -> str:
    note = evidence_note(finding)
    return f" ({note})" if note else ""


def _gaps_section(spec: dict[str, Any]) -> list[str]:
    lines = [
        "## Thin evidence",
        "",
        "_What the recording never covered. Each row names the workload addition that would close it._",
        "",
    ]
    if "evidence_gaps" not in spec:
        return lines + ["_Not computed for this spec; run `infer` again._", ""]
    gaps = spec["evidence_gaps"]
    if not gaps:
        return lines + ["_None detected._", ""]
    lines += ["| kind | where | detail | suggested workload addition |", "| --- | --- | --- | --- |"]
    for gap in gaps:
        lines.append(
            f"| {gap['kind']} | {_cell(gap['where'])} | {_cell(gap['detail'])} | {_cell(gap['suggestion'])} |"
        )
    return lines + [""]


def _flows_section(flows: list[dict[str, Any]]) -> list[str]:
    lines = ["## Identifier flows", ""]
    lines += [f"- {f['description']}" for f in flows] or ["_None detected._"]
    return lines + [""]


def _claims_section(claims: list[dict[str, Any]]) -> list[str]:
    lines = ["## Documentation vs reality", ""]
    if not claims:
        return lines + ["_No claims supplied._", ""]
    ordered = sorted(claims, key=lambda c: _VERDICT_ORDER.get(c["verdict"], 99))
    lines += ["| id | claim | verdict | evidence |", "| --- | --- | --- | --- |"]
    for claim in ordered:
        lines.append(
            f"| {_cell(claim['id'])} | {_cell(claim['text'])} | {claim['verdict']} | {_cell(_evidence(claim))} |"
        )
    return lines + [""]


def _endpoint_section(endpoint: dict[str, Any]) -> list[str]:
    lines = [f"### {endpoint['endpoint']}", "", f"{endpoint['count']} recorded requests.", ""]
    if endpoint["params"]:
        lines += ["| param | types | required | examples |", "| --- | --- | --- | --- |"]
        for name, info in endpoint["params"].items():
            examples = ", ".join(_cell(e) for e in info["examples"])
            lines.append(
                f"| {_cell(name)} | {', '.join(info['types'])} | "
                f"{'yes' if info['required'] else 'no'} | {examples} |"
            )
        lines.append("")
    lines += ["| status | content type | count | fields |", "| --- | --- | --- | --- |"]
    for shape in endpoint["responses"]:
        if shape["json_keys"] is None:
            fields = "text"
        else:
            types = shape["field_types"] or {}
            fields = ", ".join(f"{k}: {'/'.join(types.get(k, []))}" for k in shape["json_keys"])
        lines.append(
            f"| {shape['status']} | {_cell(shape['content_type'])} | {shape['count']} | {_cell(fields)} |"
        )
    return lines + [""]


def render_report(spec: dict[str, Any]) -> str:
    """Render the spec as a human-readable markdown behaviour report."""
    claims = spec.get("claims", [])
    verdicts = Counter(c["verdict"] for c in claims)
    hidden = spec["boundaries"] + spec["normalization"]
    lines = [
        "# Behaviour report",
        "",
        f"{spec['trace_count']} traces, {len(spec['endpoints'])} endpoints, "
        f"{len(hidden)} hidden behaviours. Claims: {verdicts['CONTRADICTED']} contradicted, "
        f"{verdicts['CONFIRMED']} confirmed, {verdicts['UNOBSERVED']} unobserved.",
        "",
    ]
    lines += _claims_section(claims)

    lines += ["## Hidden behaviour", ""]
    if hidden:
        lines.append(
            "_Points are the samples in the sweep. A change tag is `exact` when the samples either side "
            "of it are adjacent, or `±N` when N unsampled inputs could hide the exact edge._"
        )
        lines.append("")
    lines += [f"- {f['description']}{_note_suffix(f)}" for f in hidden] or ["_None detected._"]
    lines.append("")
    lines += _gaps_section(spec)

    lines += _flows_section(spec.get("flows", []))

    lines += ["## Volatile fields", ""]
    volatile_lines = [
        f"- `{key}` `{f['path']}`: {f['reason']}"
        for key, findings in sorted(spec["volatile_fields"].items())
        for f in findings
    ]
    lines += volatile_lines or ["_None detected._"]
    lines.append("")

    lines += ["## Endpoints", ""]
    for endpoint in spec["endpoints"]:
        lines += _endpoint_section(endpoint)
    return "\n".join(lines).rstrip() + "\n"


def save_spec(spec: dict[str, Any], path: str | Path) -> None:
    """Write the spec as JSON with sorted keys."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_spec(path: str | Path) -> dict[str, Any]:
    """Read a spec written by :func:`save_spec`."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
