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

Stateful systems are probed with scenario runs (:func:`probe_scenarios`).
Each run executes its steps on both systems in lockstep, each side with its
own captured identifiers, and an :class:`~archaeology.replay.IdentityMap`
translates the reference's identifiers before responses are compared.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from .infer.spec import volatile_paths_for
from .replay.compare import compare
from .replay.identity import IdentityMap
from .scenarios import ScenarioRun, apply_captures, render_step
from .trace import FORM_CONTENT_TYPE, Request, Response, Trace

Target = Callable[[Request], Response]

_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


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
    """Generate ``count`` requests spread across the spec's parameterised endpoints.

    Pass only stateless traces (``scenario is None``): a scenario step's
    parameters are identifiers issued by earlier steps, which a fresh request
    cannot reuse.
    """
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
    """One exchange where the rebuild disagreed with the legacy system.

    ``scenario`` is the step's ``Trace.scenario`` tag for a scenario step, or
    ``None`` for a standalone request. ``path_template`` is a scenario step's
    path as written in the workload, such as ``/loans/{{loan}}/return``.
    """

    request: Request
    differences: list[str]
    scenario: dict[str, Any] | None = None
    path_template: str | None = None


@dataclass
class ProbeReport:
    """Results of a probe. ``total`` counts fresh requests and scenario steps alike."""

    total: int = 0
    scenario_steps: int = 0
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def agreement(self) -> float:
        return 1.0 if self.total == 0 else (self.total - len(self.divergences)) / self.total


def merge_reports(*reports: ProbeReport) -> ProbeReport:
    """One report holding the totals and divergences of several."""
    merged = ProbeReport()
    for report in reports:
        merged.total += report.total
        merged.scenario_steps += report.scenario_steps
        merged.divergences.extend(report.divergences)
    return merged


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


def probe_scenarios(
    reference: Target,
    candidate: Target,
    runs: Iterable[ScenarioRun],
    spec: dict[str, Any] | None = None,
) -> ProbeReport:
    """Run each scenario on both systems in lockstep and compare every step.

    Each side renders the steps with its own variables and applies its own
    captures, so the candidate always uses identifiers it issued. Before a
    step's reference response is compared, identifiers the reference issued
    earlier in the run are translated into the candidate's; the volatile fields
    of each step (as the spec names them) are then learned into the run's
    identity map. Each run gets its own identity map.
    """
    report = ProbeReport()
    for run in runs:
        identity = IdentityMap()
        reference_vars = dict(run.bindings)
        candidate_vars = dict(run.bindings)
        for index, step in enumerate(run.steps):
            reference_request = render_step(step, reference_vars)
            candidate_request = render_step(step, candidate_vars)
            reference_response = reference(reference_request)
            candidate_response = candidate(candidate_request)
            apply_captures(step, reference_response, reference_vars)
            apply_captures(step, candidate_response, candidate_vars)

            ignore = volatile_paths_for(spec, reference_request) if spec else set()
            expected = identity.rewrite_expected(reference_response)
            differences = compare(expected, candidate_response, ignore)
            report.total += 1
            report.scenario_steps += 1
            if differences:
                report.divergences.append(
                    Divergence(
                        reference_request,
                        differences,
                        scenario=run.tag(index),
                        path_template=step["path"],
                    )
                )
            identity.learn(reference_response, candidate_response, ignore)
    return report


def _scenario_label(scenario: dict[str, Any]) -> str:
    varied = ", ".join(f"{k}={v}" for k, v in sorted(scenario.get("vars", {}).items()))
    label = f"`{scenario['name']}` step {scenario['step']}"
    return f"{label} with {varied}" if varied else label


def _divergence_line(divergence: Divergence) -> str:
    request = divergence.request
    differences = "; ".join(divergence.differences)
    if divergence.scenario is None:
        return f"- `{request.method} {request.path}` `{request.query_string or request.body}`: {differences}"
    shown = _PLACEHOLDER.sub("{...}", divergence.path_template or request.path)
    return f"- {_scenario_label(divergence.scenario)}: `{request.method} {shown}`: {differences}"


def render_markdown(report: ProbeReport, max_items: int = 20) -> str:
    agree = report.total - len(report.divergences)
    subject = "fresh requests and scenario steps" if report.scenario_steps else "fresh requests"
    lines = [
        "# Differential probe report",
        "",
        f"**{agree} / {report.total} {subject} agree ({report.agreement:.1%})**",
        "",
        f"Verdict: {'NO DIVERGENCE FOUND' if not report.divergences else 'DIVERGENT'}",
    ]
    if report.divergences:
        lines += ["", "## Divergences", ""]
        for d in report.divergences[:max_items]:
            lines.append(_divergence_line(d))
        if len(report.divergences) > max_items:
            lines.append(f"- ... and {len(report.divergences) - max_items} more")
    return "\n".join(lines) + "\n"


def to_dict(report: ProbeReport) -> dict[str, Any]:
    """A JSON-serialisable summary of the report, with its divergences."""
    return {
        "total": report.total,
        "scenario_steps": report.scenario_steps,
        "agreement": report.agreement,
        "divergences": [
            {
                "request": f"{d.request.method} {d.request.path}",
                "query_or_body": d.request.query_string or d.request.body,
                "scenario": d.scenario,
                "differences": list(d.differences),
            }
            for d in report.divergences
        ],
    }
