"""Refine: turn a recording's thin evidence into workload additions, record them, and infer again.

For each evidence gap the engine can act on, :func:`refine` writes the workload
additions that would close it:

* ``inexact_change``: a sweep of every unsampled input strictly between the two
  samples that bracket the change, once per context that shares the bracket.
  For a scenario variable, one copy of the scenario template per context, with
  the unsampled values as its ``values`` vary (``late-return~refine1``).
* ``single_status``: requests that try an unknown path id in the format the
  catalog inferred (same prefix, length and charset as the observed ids), each
  invalid value already seen for that parameter name, and the request with each
  required parameter left out.
* ``few_values``: one request or run with a third value: a value a claim names
  for that parameter, else an obviously invalid value.

``failed_capture`` (a placeholder in the workload itself) and
``unobserved_claim`` (a claim that needs targeted probing) are not acted on;
the log gives the reason.

Each round records the additions against the target, appends the traces,
infers the spec again and recomputes the gaps. A gap that no longer appears is
*closed*. A gap that remains after its additions ran is *explained*: the log
quotes the statuses or values the additions actually saw.

The target must be fresh and the traces a full recording of the workload (what
``record`` writes, not ``explore``). Refine first replays the workload silently,
so the target is in the state the recording was made in, and then records only
the additions. The appended traces therefore continue the recording in order,
which a stateful system needs. The refined workload lists the additions in its
own sections, so a fresh run of it sends the same requests in another order.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from .explore import full_request_count
from .infer.catalog import endpoint_key
from .infer.spec import infer_spec
from .recorder.exerciser import expand_sweep, expand_workload, request_from_spec
from .scenarios import expand_scenarios, run_scenario, scenario_family
from .trace import Request, Response, Trace

Target = Callable[[Request], Response]
Addition = dict[str, list[Any]]

ACTIONABLE = ("inexact_change", "few_values", "single_status")
KINDS = ("inexact_change", "few_values", "single_status", "failed_capture", "unobserved_claim")
NOT_ACTED_ON = {
    "failed_capture": "a placeholder in the workload itself: a defect for its author to fix, not a behaviour gap",
    "unobserved_claim": "a claim needs targeted probing, which refine does not send",
}
WRITE_METHODS = ("POST", "PUT", "PATCH")
INVALID_VALUE = "refine-invalid"
UNKNOWN_ID_COUNT = 2
INVALID_LIMIT = 3
LISTED = 4
_ID_PREFIX = re.compile(r"^([A-Za-z]{1,4}[-_])?(.*)$", re.S)
_DIGITS = "0123456789"
_HEX_LOWER = "0123456789abcdef"
_HEX_ANY = "0123456789abcdefABCDEF"


# --- gaps, additions and their sizes -----------------------------------------


def gap_key(gap: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a gap across rounds: its kind, where, and the bracket of an inexact change."""
    data = gap.get("data") or {}
    return (gap["kind"], gap["where"], data.get("lo"), data.get("hi"))


def empty_addition() -> Addition:
    return {"requests": [], "sweeps": [], "scenarios": []}


def merge_addition(into: Addition, other: Addition) -> None:
    for key in ("requests", "sweeps", "scenarios"):
        into.setdefault(key, []).extend(other.get(key, []))


def addition_size(addition: Addition) -> int:
    """How many exchanges an addition sends."""
    return full_request_count(addition)


def _listed(items: Sequence[str]) -> str:
    shown = ", ".join(items[:LISTED])
    return shown + (" and more" if len(items) > LISTED else "")


def _template(workload: dict[str, Any], name: str) -> dict[str, Any]:
    for template in workload.get("scenarios", []):
        if template["name"] == name:
            return template
    raise KeyError(f"scenario {name!r} is not in the workload")


# --- context: what the generators may read -------------------------------------


@dataclass
class Context:
    """What an addition generator reads: the traces so far, the spec, the workload and the claims."""

    traces: list[Trace]
    spec: dict[str, Any]
    workload: dict[str, Any]
    claims: list[dict[str, Any]]
    numbers: Iterator[int] = field(default_factory=lambda: itertools.count(1))


def _variant(template: dict[str, Any], ctx: Context, overrides: dict[str, Any],
             context: dict[str, str]) -> dict[str, Any]:
    """A copy of a scenario template under a new name, with its vary fixed to ``context`` plus ``overrides``.

    A variable the context does not name is null in the copy, so it is omitted from its requests,
    as it was in the run the context came from.
    """
    variant = copy.deepcopy(template)
    variant["name"] = f"{template['name']}~refine{next(ctx.numbers)}"
    names = dict.fromkeys([*template.get("vary", {}), *overrides])
    variant["vary"] = {
        name: overrides[name] if name in overrides
        else {"values": [context[name]]} if name in context
        else {"values": [None]}
        for name in names
    }
    return variant


# --- inexact changes -----------------------------------------------------------


def _between(axis: str, lo: str, hi: str) -> list[str]:
    """Every input strictly between two sampled inputs, as the workload writes it."""
    if axis == "date":
        start, stop = date.fromisoformat(lo).toordinal(), date.fromisoformat(hi).toordinal()
        return [date.fromordinal(day).isoformat() for day in range(start + 1, stop)]
    return [str(value) for value in range(int(lo) + 1, int(hi))]


def inexact_addition(data: dict[str, Any], ctx: Context) -> tuple[Addition, str]:
    values = _between(data["axis"], data["lo"], data["hi"])
    contexts = data.get("contexts") or []
    addition = empty_addition()
    if not values or not contexts:
        return addition, ""
    param = data["param"]
    if data.get("scenario") is not None:
        template = _template(ctx.workload, data["scenario"])
        for context in contexts:
            addition["scenarios"].append(_variant(template, ctx, {param: {"values": values}}, context))
        text = f"{len(contexts)} run variant(s) of scenario {data['scenario']} with {param} at {_listed(values)}"
    else:
        for context in contexts:
            addition["sweeps"].append({
                "method": data["method"],
                "path": data["path"],
                data.get("location", "query"): dict(context),
                "vary": {param: {"values": values}},
            })
        text = f"sweep of {param} at {_listed(values)} in {len(contexts)} context(s)"
    return addition, text


# --- few values ----------------------------------------------------------------


def _third_value(param: str, scenario: str | None, claims: Sequence[dict[str, Any]], seen: set[str]) -> str:
    """A value a claim names for ``param`` that is not seen yet, else an obviously invalid one."""
    for claim in claims:
        request = claim.get("request") or {}
        if scenario is not None:
            if request.get("scenario") != scenario:
                continue
            named = request.get("vars") or {}
        else:
            named = {**(request.get("query") or {}), **(request.get("form") or {})}
        if param in named and str(named[param]) not in seen:
            return str(named[param])
    return INVALID_VALUE


def few_values_addition(data: dict[str, Any], ctx: Context) -> tuple[Addition, str]:
    param, seen = data["param"], set(data["seen"])
    scenario = data["scenario"] if data["kind"] == "scenario" else None
    value = _third_value(param, scenario, ctx.claims, seen)
    addition = empty_addition()
    if scenario is not None:
        template = _template(ctx.workload, scenario)
        sample = next(t for t in ctx.traces
                      if t.scenario is not None and scenario_family(str(t.scenario["name"])) == scenario
                      and param in (t.scenario.get("vars") or {}))
        context = {k: str(v) for k, v in (sample.scenario.get("vars") or {}).items() if k != param}
        addition["scenarios"].append(_variant(template, ctx, {param: {"values": [value]}}, context))
        return addition, f"one run of scenario {scenario} with {param} set to {value!r}"
    sample = next(t for t in ctx.traces
                  if t.scenario is None and endpoint_key(t) == data["endpoint"] and param in t.request.params)
    params = {k: v for k, v in sample.request.params.items() if k != param}
    params[param] = value
    addition["requests"].append({"method": data["method"], "path": sample.request.path,
                                 data.get("location", "query"): params})
    return addition, f"one request with {param} set to {value!r}"


# --- single statuses -----------------------------------------------------------


def _alphabet(bodies: Sequence[str]) -> str:
    """The charset of observed ids: digits, lower or mixed-case hex, else the characters seen."""
    text = set("".join(bodies))
    for alphabet in (_DIGITS, _HEX_LOWER, _HEX_ANY):
        if text <= set(alphabet):
            return alphabet
    return "".join(sorted(text))


def _digest_text(seed: str, length: int, alphabet: str) -> str:
    chars: list[str] = []
    block = 0
    while len(chars) < length:
        digest = hashlib.sha256(f"{seed}:{block}".encode("utf-8")).digest()
        chars.extend(alphabet[byte % len(alphabet)] for byte in digest)
        block += 1
    return "".join(chars[:length])


def unknown_ids(samples: Sequence[str], observed: set[str], count: int = UNKNOWN_ID_COUNT) -> list[str]:
    """Ids in the observed format (prefix, length, charset) that no trace has used."""
    parts = [(m.group(1) or "", m.group(2)) for m in (_ID_PREFIX.match(s) for s in samples) if m]
    bodies = [body for _, body in parts if body]
    if not bodies:
        return []
    prefix = Counter(p for p, _ in parts).most_common(1)[0][0]
    length = Counter(len(b) for b in bodies).most_common(1)[0][0]
    alphabet = _alphabet(bodies)
    found: list[str] = []
    attempt = 0
    while len(found) < count and attempt < 10_000:
        candidate = prefix + _digest_text(f"refine-unknown-{attempt}", length, alphabet)
        attempt += 1
        if candidate not in observed and candidate not in found:
            found.append(candidate)
    return found


def _id_param_name(segments: Sequence[str], position: int) -> str:
    """The parameter an id segment stands for, named after the segment before it: ``/customers/{id}`` gives ``customer_id``."""
    previous = segments[position - 1] if position > 0 else ""
    stem = previous[:-1] if previous.endswith("s") and len(previous) > 1 else previous
    return f"{stem}_id"


def _named_values(trace: Trace, name: str) -> list[tuple[str, int]]:
    """Values a trace sent for a parameter name, as a parameter or as an id in its path.

    A ``{{placeholder}}`` left unbound by a scenario is not a value anyone sent on purpose.
    """
    pairs: list[tuple[str, int]] = []
    status = trace.response.status
    if name in trace.request.params:
        pairs.append((trace.request.params[name], status))
    segments = trace.request.path.split("/")
    for position in range(1, len(segments)):
        if segments[position] and _id_param_name(segments, position) == name:
            pairs.append((segments[position], status))
    return [(value, code) for value, code in pairs if value and "{{" not in value]


def invalid_values(name: str, traces: Sequence[Trace], limit: int = INVALID_LIMIT) -> list[str]:
    """Values seen for ``name`` that drew an error and never a success, in first-seen order."""
    invalid: list[str] = []
    valid: set[str] = set()
    for trace in traces:
        for value, status in _named_values(trace, name):
            if status >= 400:
                if value not in invalid:
                    invalid.append(value)
            else:
                valid.add(value)
    return [value for value in invalid if value not in valid][:limit]


def _observed_values(traces: Sequence[Trace]) -> set[str]:
    seen: set[str] = set()
    for trace in traces:
        seen.update(trace.request.path.split("/"))
        seen.update(trace.request.params.values())
    return seen


def _required_params(spec: dict[str, Any], endpoint: str) -> list[str]:
    for entry in spec.get("endpoints", []):
        if entry["endpoint"] == endpoint:
            return [name for name, info in entry["params"].items() if info.get("required")]
    return []


def _send_spec(method: str, path: str, params: dict[str, str], location: str) -> dict[str, Any]:
    return {"method": method, "path": path, location: params}


def single_status_addition(data: dict[str, Any], ctx: Context) -> tuple[Addition, str]:
    endpoint, method, location = data["endpoint"], data["method"], data.get("location", "query")
    group = [t for t in ctx.traces if endpoint_key(t) == endpoint]
    addition = empty_addition()
    if not group:
        return addition, ""
    sample = group[0]
    segments = sample.request.path.split("/")
    template = data["template"].split("/")
    observed = _observed_values(ctx.traces)
    parts: list[str] = []
    for position, segment in enumerate(template):
        if segment != "{id}":
            continue
        name = _id_param_name(template, position)
        samples = [t.request.path.split("/")[position] for t in group]
        unknown = unknown_ids(samples, observed)
        invalid = invalid_values(name, ctx.traces)
        for candidate in [*unknown, *invalid]:
            concrete = list(segments)
            concrete[position] = quote(candidate, safe="")
            addition["requests"].append(
                _send_spec(method, "/".join(concrete), dict(sample.request.params), location))
        if unknown:
            parts.append(f"unknown {name} {_listed(unknown)}")
        if invalid:
            parts.append(f"invalid {name} {_listed(invalid)}")
    for name in sorted({k for t in group for k in t.request.params}):
        invalid = invalid_values(name, ctx.traces)
        for value in invalid:
            params = dict(sample.request.params)
            params[name] = value
            addition["requests"].append(_send_spec(method, sample.request.path, params, location))
        if invalid:
            parts.append(f"invalid {name} {_listed(invalid)}")
    for name in _required_params(ctx.spec, endpoint):
        if name in sample.request.params:
            params = {k: v for k, v in sample.request.params.items() if k != name}
            addition["requests"].append(_send_spec(method, sample.request.path, params, location))
            parts.append(f"without {name}")
    return addition, "; ".join(parts)


# --- dispatch ------------------------------------------------------------------


_GENERATORS = {
    "inexact_change": inexact_addition,
    "few_values": few_values_addition,
    "single_status": single_status_addition,
}


def addition_for_gap(gap: dict[str, Any], ctx: Context) -> tuple[Addition | None, str]:
    """The workload addition for one gap, or ``None`` with the reason it is not acted on."""
    kind = gap["kind"]
    if kind in NOT_ACTED_ON:
        return None, NOT_ACTED_ON[kind]
    addition, text = _GENERATORS[kind](gap.get("data") or {}, ctx)
    if addition_size(addition) == 0:
        return None, "nothing to add: no unsampled input, id, parameter or value to vary"
    return addition, text


# --- recording -----------------------------------------------------------------


def _statuses_text(statuses: Sequence[int]) -> str:
    """``200 x2, 404`` for the statuses of a batch of exchanges, in order of first appearance."""
    counts = Counter(statuses)
    return ", ".join(f"{status} x{count}" if count > 1 else str(status) for status, count in counts.items())


def replay_workload(target: Target, workload: dict[str, Any]) -> None:
    """Send a workload's exchanges without recording them, to bring a target to its recording's state."""
    for request in expand_workload(workload):
        target(request)
    for run in expand_scenarios(workload):
        run_scenario(run, target)


def record_addition(addition: Addition, target: Target, traces: Sequence[Trace], prefix: str) -> list[Trace]:
    """Send an addition's requests, sweeps and scenario runs to the target and return the traces."""
    first_run = max((int(t.scenario["run"]) for t in traces if t.scenario is not None), default=-1) + 1
    recorded: list[Trace] = []

    def emit(request: Request, response: Response, scenario: dict[str, Any] | None) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        recorded.append(Trace(f"{prefix}{len(recorded) + 1:06d}", stamp, request, response, scenario))

    for spec in addition["requests"]:
        request = request_from_spec(spec)
        emit(request, target(request), None)
    for sweep in addition["sweeps"]:
        for request in expand_sweep(sweep):
            emit(request, target(request), None)
    for run in expand_scenarios({"scenarios": addition["scenarios"]}, first_run=first_run):
        run_scenario(run, target, on_exchange=emit)
    return recorded


# --- the loop ------------------------------------------------------------------


@dataclass
class Outcome:
    """What became of one gap: the round that acted on it (0 if none), what was added, and the result."""

    round: int
    gap: dict[str, Any]
    description: str
    requests: int
    status: str  # closed | explained | not acted on | not attempted
    after: str = ""
    drew: str = ""  # the statuses the gap's own additions drew, as read from their traces


@dataclass
class RefineResult:
    traces: list[Trace]
    workload: dict[str, Any]
    initial_gaps: list[dict[str, Any]]
    final_gaps: list[dict[str, Any]]
    outcomes: list[Outcome]
    rounds: int
    recorded_before: int

    @property
    def added(self) -> int:
        return len(self.traces) - self.recorded_before


def refine(
    target: Target,
    workload: dict[str, Any],
    traces: Sequence[Trace],
    claims: Sequence[dict[str, Any]] = (),
    rounds: int = 3,
) -> RefineResult:
    """Close the evidence gaps of a recording by recording workload additions against ``target``.

    ``workload`` is never modified: the refined workload is a deep copy with the additions appended.
    """
    traces = list(traces)
    claims = list(claims)
    expected = full_request_count(workload)
    if expected != len(traces):
        raise ValueError(
            f"the traces hold {len(traces)} exchanges but the workload sends {expected}; "
            "refine needs a full recording of that workload (record, not explore)"
        )
    replay_workload(target, workload)
    refined = copy.deepcopy(workload)
    for key in ("requests", "sweeps", "scenarios"):
        refined.setdefault(key, [])
    ctx = Context(traces=list(traces), spec=infer_spec(traces, claims), workload=refined, claims=claims)
    initial = ctx.spec["evidence_gaps"]
    gaps = initial
    decided: dict[tuple[Any, ...], Outcome] = {}
    rounds_run = 0

    for number in range(1, rounds + 1):
        plan: list[tuple[tuple[Any, ...], dict[str, Any], Addition, str]] = []
        for gap in gaps:
            key = gap_key(gap)
            if key in decided:
                continue
            addition, text = addition_for_gap(gap, ctx)
            if addition is None:
                decided[key] = Outcome(0, gap, text, 0, "not acted on")
            else:
                plan.append((key, gap, addition, text))
        if not plan:
            break
        rounds_run += 1
        drew: dict[tuple[Any, ...], str] = {}
        for index, (key, _, addition, _) in enumerate(plan):
            batch = record_addition(addition, target, ctx.traces, f"r{number}-{index}-")
            ctx.traces.extend(batch)
            merge_addition(refined, addition)
            drew[key] = _statuses_text([t.response.status for t in batch])
        ctx.spec = infer_spec(ctx.traces, claims)
        gaps = ctx.spec["evidence_gaps"]
        remaining = {gap_key(g): g for g in gaps}
        for key, gap, addition, text in plan:
            after = remaining.get(key)
            decided[key] = Outcome(
                number, gap, text, addition_size(addition),
                "explained" if after is not None else "closed",
                after["detail"] if after is not None else "",
                drew[key],
            )

    for gap in gaps:
        key = gap_key(gap)
        if key not in decided:
            decided[key] = Outcome(0, gap, "", 0, "not attempted")
    return RefineResult(
        traces=ctx.traces,
        workload=refined,
        initial_gaps=initial,
        final_gaps=gaps,
        outcomes=list(decided.values()),
        rounds=rounds_run,
        recorded_before=len(traces),
    )


# --- the log -------------------------------------------------------------------


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _outcome_text(outcome: Outcome) -> str:
    drew = f"; its requests drew {outcome.drew}" if outcome.drew else ""
    if outcome.status == "explained":
        return f"explained: {outcome.after}{drew}"
    if outcome.status == "closed":
        return f"closed{drew}"
    return outcome.status


def render_refine_log(result: RefineResult) -> str:
    """A markdown log: the gaps before and after, then per round each gap, what was added and its outcome."""
    before = Counter(g["kind"] for g in result.initial_gaps)
    after = Counter(g["kind"] for g in result.final_gaps)
    actioned = sum(1 for o in result.outcomes if o.round > 0)
    lines = [
        "# Refinement log",
        "",
        f"The recording held {result.recorded_before} exchanges and {len(result.initial_gaps)} evidence gaps. "
        f"Refinement ran {result.rounds} round(s), recorded {result.added} additional exchanges, "
        f"acted on {actioned} gap(s) and left {len(result.final_gaps)} gap(s).",
        "",
        "Closed means the gap no longer appears after its additions. Explained means the additions ran "
        "and the gap remains; the outcome quotes what they saw.",
        "",
        "## Gaps before and after",
        "",
        "| Kind | Before | After |",
        "|---|---:|---:|",
    ]
    lines += [f"| {kind} | {before[kind]} | {after[kind]} |" for kind in KINDS]
    for number in range(1, result.rounds + 1):
        rows = [o for o in result.outcomes if o.round == number]
        lines += ["", f"## Round {number}", "",
                  "| Kind | Where | Gap | Addition | Requests | Outcome |", "|---|---|---|---|---:|---|"]
        lines += [
            f"| {o.gap['kind']} | {_cell(o.gap['where'])} | {_cell(o.gap['detail'])} | "
            f"{_cell(o.description)} | {o.requests} | {_cell(_outcome_text(o))} |"
            for o in rows
        ]
    skipped = [o for o in result.outcomes if o.round == 0 or o.status == "not attempted"]
    lines += ["", "## Not acted on", ""]
    if skipped:
        lines += ["| Kind | Where | Gap | Reason | Outcome |", "|---|---|---|---|---|"]
        lines += [
            f"| {o.gap['kind']} | {_cell(o.gap['where'])} | {_cell(o.gap['detail'])} | "
            f"{_cell(o.description)} | {_cell(o.status)} |"
            for o in skipped
        ]
    else:
        lines.append("_None._")
    lines += ["", "## Gaps left after refinement", ""]
    if result.final_gaps:
        lines += ["| Kind | Where | Detail |", "|---|---|---|"]
        lines += [f"| {g['kind']} | {_cell(g['where'])} | {_cell(g['detail'])} |" for g in result.final_gaps]
    else:
        lines.append("_None._")
    lines += [
        "",
        "## Notes",
        "",
        "- The workload is never modified. The refined workload appends each addition to its own section, "
        "so a fresh run of it sends the same requests, but in a different order.",
        "- Additions were recorded after the whole original workload, as the traces show. A stateful "
        "system therefore saw them in that order.",
        "",
    ]
    return "\n".join(lines)
