"""Assemble inferred behaviour into a spec, a readable report, and JSON on disk."""

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
from .normalization import detect_normalization
from .volatility import detect_volatile_fields, volatile_paths

SPEC_VERSION = 1
_VERDICT_ORDER = {"CONTRADICTED": 0, "CONFIRMED": 1, "UNOBSERVED": 2}


def infer_spec(traces: Sequence[Trace], claims: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Infer the full behavioural spec from recorded traces and optional claims."""
    traces = list(traces)
    volatile = detect_volatile_fields(traces)
    return {
        "version": SPEC_VERSION,
        "trace_count": len(traces),
        "endpoints": build_catalog(traces),
        "volatile_fields": volatile,
        "boundaries": detect_boundaries(traces),
        "normalization": detect_normalization(traces, volatile),
        "claims": check_claims(claims, traces) if claims else [],
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
    lines += [f"- {f['description']}" for f in hidden] or ["_None detected._"]
    lines.append("")

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
