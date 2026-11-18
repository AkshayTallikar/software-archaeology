"""Differential probing: test a rebuild on inputs nobody recorded.

Replay proves equivalence on the traffic that was recorded. That is only as
strong as the recording: a rebuild can pass replay at 100% and still differ
on inputs the recording never exercised. When the legacy system can still be
called, probing closes that gap by generating fresh requests from the
inferred spec and sending each one to both systems.

Requests are generated per endpoint from the spec's catalog: every observed
parameter is included or omitted at random (required ones always included),
string parameters take one of their observed values, and integer parameters
take a value drawn across their observed range. The same seed always
produces the same requests.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from .infer.spec import volatile_paths_for
from .replay.compare import compare
from .trace import FORM_CONTENT_TYPE, Request, Response, Trace

Target = Callable[[Request], Response]


def _int_range(values: Iterable[str]) -> tuple[int, int] | None:
    ints = []
    for v in values:
        try:
            ints.append(int(v))
        except ValueError:
            continue
    return (min(ints), max(ints)) if ints else None


def observed_values(traces: Iterable[Trace]) -> dict[str, dict[str, list[str]]]:
    """All distinct raw values seen per endpoint key and parameter."""
    from .infer.catalog import endpoint_key

    seen: dict[str, dict[str, list[str]]] = {}
    for trace in traces:
        params = seen.setdefault(endpoint_key(trace), {})
        for name, value in trace.request.params.items():
            bucket = params.setdefault(name, [])
            if value not in bucket:
                bucket.append(value)
    return seen


def generate_requests(
    spec: dict[str, Any],
    traces: Iterable[Trace],
    count: int,
    seed: int = 0,
    form_endpoints: Iterable[str] | None = None,
) -> list[Request]:
    """Generate ``count`` requests spread across the spec's parameterised endpoints."""
    rng = random.Random(seed)
    traces = list(traces)
    values = observed_values(traces)
    forms = set(form_endpoints or ())
    for trace in traces:
        if trace.request.content_type == FORM_CONTENT_TYPE:
            from .infer.catalog import endpoint_key

            forms.add(endpoint_key(trace))

    endpoints = [ep for ep in spec.get("endpoints", []) if ep.get("params") and "{" not in ep["path"]]
    if not endpoints:
        return []

    requests: list[Request] = []
    for i in range(count):
        ep = endpoints[i % len(endpoints)]
        key = ep["endpoint"]
        params: dict[str, str] = {}
        for name, info in ep["params"].items():
            if not info.get("required") and rng.random() < 0.25:
                continue
            pool = values.get(key, {}).get(name, [])
            span = _int_range(pool)
            if "int" in info.get("types", []) and span and rng.random() < 0.8:
                lo, hi = span
                params[name] = str(rng.randint(lo, hi))
            elif pool:
                params[name] = rng.choice(pool)
        encoded = urlencode(params)
        if key in forms:
            requests.append(
                Request.build(ep["method"], ep["path"], body=encoded,
                              headers={"Content-Type": FORM_CONTENT_TYPE})
            )
        else:
            requests.append(Request.build(ep["method"], ep["path"], query_string=encoded))
    return requests


@dataclass
class Divergence:
    request: Request
    differences: list[str]


@dataclass
class ProbeReport:
    total: int = 0
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def agreement(self) -> float:
        return 1.0 if self.total == 0 else (self.total - len(self.divergences)) / self.total


def probe(
    reference: Target,
    candidate: Target,
    requests: Iterable[Request],
    spec: dict[str, Any] | None = None,
) -> ProbeReport:
    """Send each request to both systems and collect every divergence."""
    report = ProbeReport()
    for request in requests:
        ignore = volatile_paths_for(spec, request) if spec else set()
        differences = compare(reference(request), candidate(request), ignore)
        report.total += 1
        if differences:
            report.divergences.append(Divergence(request, differences))
    return report


def render_markdown(report: ProbeReport, max_items: int = 20) -> str:
    agree = report.total - len(report.divergences)
    lines = [
        "# Differential probe report",
        "",
        f"**{agree} / {report.total} fresh requests agree ({report.agreement:.1%})**",
        "",
        f"Verdict: {'NO DIVERGENCE FOUND' if not report.divergences else 'DIVERGENT'}",
    ]
    if report.divergences:
        lines += ["", "## Divergences", ""]
        for d in report.divergences[:max_items]:
            req = d.request
            lines.append(f"- `{req.method} {req.path}` `{req.query_string or req.body}`: " + "; ".join(d.differences))
        if len(report.divergences) > max_items:
            lines.append(f"- ... and {len(report.divergences) - max_items} more")
    return "\n".join(lines) + "\n"
