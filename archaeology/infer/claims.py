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


def _check_one(claim: dict[str, Any], traces: Sequence[Trace]) -> dict[str, Any]:
    request = claim["request"]
    method = str(request["method"]).upper()
    path = request["path"]
    params = _claim_params(request)
    result: dict[str, Any] = {
        "id": claim["id"],
        "text": claim["text"],
        "verdict": "UNOBSERVED",
        "trace_id": None,
        "observed": None,
        "mismatches": [],
    }
    match = next((t for t in traces if _matches(t, method, path, params)), None)
    if match is None:
        return result
    status, body = match.response.status, match.response.body
    result["trace_id"] = match.id
    result["observed"] = {"status": status, "body": body[:OBSERVED_BODY_LIMIT]}
    result["mismatches"] = _mismatches(claim.get("expect", {}), status, body)
    result["verdict"] = "CONTRADICTED" if result["mismatches"] else "CONFIRMED"
    return result


def check_claims(claims: Sequence[dict[str, Any]], traces: Sequence[Trace]) -> list[dict[str, Any]]:
    """Check each claim against the first trace with the same method, path and params."""
    return [_check_one(claim, traces) for claim in claims]
