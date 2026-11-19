"""Documented claims checked against the recorded traces."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..trace import Request, Trace
from .catalog import parse_body

OBSERVED_BODY_LIMIT = 300


def load_claims(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSON list of claims."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _claim_params(request: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, Any] = {}
    merged.update(request.get("query") or {})
    merged.update(request.get("form") or {})
    return {str(k): str(v) for k, v in merged.items()}


def _matches(trace: Trace, method: str, path: str, params: dict[str, str]) -> bool:
    request: Request = trace.request
    return request.method == method and request.path == path and request.params == params


def _in_scenario_step(trace: Trace, claim_request: dict[str, Any]) -> bool:
    """True when a scenario trace is the named step and carries every named variable value."""
    if trace.scenario is None or trace.scenario.get("name") != claim_request["scenario"]:
        return False
    if int(trace.scenario.get("step", -1)) != int(claim_request["step"]):
        return False
    variables = trace.scenario.get("vars") or {}
    return all(
        name in variables and str(variables[name]) == str(value)
        for name, value in (claim_request.get("vars") or {}).items()
    )


def _mismatches(expect: dict[str, Any], status: int, body: str) -> list[str]:
    problems: list[str] = []
    if "status" in expect and expect["status"] != status:
        problems.append(f"status: expected {expect['status']}, observed {status}")
    if "json_subset" in expect:
        ok, parsed = parse_body(body)
        obj = parsed if ok and isinstance(parsed, dict) else {}
        for key, want in expect["json_subset"].items():
            if key not in obj or obj[key] != want:
                seen = repr(obj[key]) if key in obj else "<missing>"
                problems.append(f"{key}: expected {want!r}, observed {seen}")
    if "body_equals" in expect and expect["body_equals"] != body:
        problems.append(
            f"body: expected {expect['body_equals']!r}, observed {body[:OBSERVED_BODY_LIMIT]!r}"
        )
    return problems


def _matching_traces(request: dict[str, Any], traces: Sequence[Trace]) -> list[Trace]:
    """Every trace whose request is the claim's request, in recording order."""
    if "scenario" in request:
        return [t for t in traces if _in_scenario_step(t, request)]
    method = str(request.get("method", "")).upper()
    path = request.get("path", "")
    params = _claim_params(request)
    return [t for t in traces if _matches(t, method, path, params)]


def _check_one(claim: dict[str, Any], traces: Sequence[Trace]) -> dict[str, Any]:
    """Judge one claim by its first matching trace, and count how the matching traces agree with it."""
    request = claim["request"]
    result: dict[str, Any] = {
        "id": claim["id"],
        "text": claim["text"],
        "request": request,
        "verdict": "UNOBSERVED",
        "trace_id": None,
        "observed": None,
        "mismatches": [],
        "support": {"matching": 0, "contradicting": 0},
    }
    expect = claim.get("expect", {})
    matches = _matching_traces(request, traces)
    for trace in matches:
        if _mismatches(expect, trace.response.status, trace.response.body):
            result["support"]["contradicting"] += 1
        else:
            result["support"]["matching"] += 1
    if not matches:
        return result
    match = matches[0]
    status, body = match.response.status, match.response.body
    result["trace_id"] = match.id
    result["observed"] = {"status": status, "body": body[:OBSERVED_BODY_LIMIT]}
    result["mismatches"] = _mismatches(expect, status, body)
    result["verdict"] = "CONTRADICTED" if result["mismatches"] else "CONFIRMED"
    return result


def check_claims(claims: Sequence[dict[str, Any]], traces: Sequence[Trace]) -> list[dict[str, Any]]:
    """Check each claim against its first matching trace.

    A claim's request is either a method, path and parameters, or a scenario
    step: ``{"scenario": name, "step": index, "vars": {...}}``, where ``vars`` is
    optional and matches the run's varied values as strings.

    Each result also carries ``support``: how many traces with the claim's
    request agree with it (``matching``) and how many contradict it
    (``contradicting``). The verdict still rests on the first matching trace.
    """
    return [_check_one(claim, traces) for claim in claims]
