"""Input normalization: which string inputs the system treats as equivalent."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations
from typing import Any

from ..trace import Trace
from .catalog import group_by_endpoint, parse_body
from .volatility import flatten_json, volatile_paths

# (pair kind) -> (behaviour if equivalent, behaviour if not, what the pairs differ by)
_BEHAVIOURS = {
    "case": ("case-insensitive", "case-sensitive", "differing only by case"),
    "whitespace": (
        "whitespace-trimmed",
        "whitespace-significant",
        "differing only by surrounding whitespace",
    ),
}


def _pair_kind(v: str, w: str) -> str | None:
    """Classify two different raw values as a case or whitespace variant, else None."""
    if v == w:
        return None
    if v.lower() == w.lower() and v == v.strip() and w == w.strip():
        return "case"
    if v.strip() == w.strip():
        return "whitespace"
    return None


def _response_signature(trace: Trace, ignore: set[str]) -> tuple[Any, ...]:
    """Comparable form of a response: status plus flattened JSON (volatile paths removed) or body."""
    ok, value = parse_body(trace.response.body)
    if ok:
        flat = {p: v for p, v in flatten_json(value).items() if p not in ignore}
        return ("json", trace.response.status, flat)
    return ("text", trace.response.status, trace.response.body)


def _param_findings(
    endpoint: str,
    param: str,
    group: Sequence[Trace],
    signatures: dict[int, tuple[Any, ...]],
) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[Trace]] = {}
    for trace in group:
        if param not in trace.request.params:
            continue
        raw = trace.request.params[param]
        others = tuple(sorted((k, v) for k, v in trace.request.params.items() if k != param))
        buckets.setdefault((others, raw.strip().lower()), []).append(trace)

    outcomes: dict[str, list[bool]] = {"case": [], "whitespace": []}
    for members in buckets.values():
        for a, b in combinations(members, 2):
            kind = _pair_kind(a.request.params[param], b.request.params[param])
            if kind is not None:
                same = signatures[id(a)] == signatures[id(b)]
                outcomes[kind].append(same)

    findings: list[dict[str, Any]] = []
    for kind, results in outcomes.items():
        if not results:
            continue
        total, same = len(results), sum(results)
        good, bad, what = _BEHAVIOURS[kind]
        if same == total:
            behaviour = good
            tail = f"{total} request pairs {what} gave identical responses"
        elif same == 0:
            behaviour = bad
            tail = f"{total} request pairs {what} gave different responses"
        else:
            behaviour = "inconsistent"
            tail = f"{same} of {total} request pairs {what} gave identical responses"
        findings.append(
            {
                "endpoint": endpoint,
                "param": param,
                "behavior": behaviour,
                "evidence_pairs": total,
                "equivalent_pairs": same,
                "description": f"{endpoint}: {param} is {behaviour} ({tail})",
            }
        )
    return findings


def detect_normalization(
    traces: Sequence[Trace], volatile: dict[str, list[dict[str, str]]] | None = None
) -> list[dict[str, Any]]:
    """Find parameters whose case or surrounding whitespace does not change the response.

    Pairs of requests that differ only in one parameter's value (same other
    parameters) are classified as case or whitespace variants and checked for
    equivalent responses. Output is sorted by (endpoint, param, behavior).
    """
    volatile = volatile or {}
    findings: list[dict[str, Any]] = []
    for endpoint, group in sorted(group_by_endpoint(traces).items()):
        ignore = volatile_paths(volatile, endpoint)
        signatures = {id(t): _response_signature(t, ignore) for t in group}
        names = sorted({name for t in group for name in t.request.params})
        for param in names:
            findings.extend(_param_findings(endpoint, param, group, signatures))
    findings.sort(key=lambda f: (f["endpoint"], f["param"], f["behavior"]))
    return findings
