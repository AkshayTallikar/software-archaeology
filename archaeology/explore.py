"""Explore: find a legacy system's behaviour with adaptive questions instead of a full sweep.

``record`` sends every value a workload varies over. Most of those values ask
the same question again: the bulk discount is 10% for every quantity from 101
to 499, so nearly all of the 400 requests in that range add nothing that a
boundary search needs. ``explore`` treats the seed workload as a description of
the domain and asks fewer, better questions:

* explicit requests, and sweeps or scenarios whose ``vary`` holds only
  ``values`` lists, are cheap, so they are sent exhaustively, as ``record``
  sends them;
* a sweep or scenario that varies an integer ``range`` or a ``date_range`` is
  explored along that axis. Its other varied keys form contexts, and every
  context gets one exploration over the axis.

Exploring one axis in one context samples the grid of values and refines only
where the response may change:

1. Coarse: ``COARSE_POINTS`` evenly spaced grid values, both ends included.
2. A coarse interval is clean when its endpoints have the same non-numeric
   values and every numeric field continues the straight line of the sample
   before it (the slope of the previous pair of samples). A change inside the
   interval breaks that continuation, so the far endpoint exposes it. Where no
   sample lies before the interval (the start of the grid), the line after it
   is used instead, and the midpoint must then also lie on the chord: a
   straight line can pass through both endpoints while the change sits inside.
3. A change is located by halving. When both neighbours fix a line for each
   side of the interval, a probe that fits the left line lies left of the
   change and a probe that fits the right line lies right of it, so each probe
   halves the interval. When neither line fits, or a neighbour is missing, the
   interval is split at its midpoint and each half is tested by the bisection
   rule: its midpoint must agree with the chord between its endpoints and
   continue the line before the half.
4. Every located change is a pair of adjacent grid values. They are the same
   values the full sweep compares, so the change points and linear segment
   boundaries that ``detect_boundaries`` reports on explored traces match the
   full recording.

Robustness, which the exploration relies on:

* Volatile fields. The first probe of each endpoint or scenario is repeated once,
  as a fresh exchange. Fields whose values differ between two identical
  requests (generated ids, timestamps, counters) are dropped from every
  signature. Non-numeric strings that look like generated ids
  (``catalog._is_opaque_id``) are dropped too. Numeric-looking strings count as
  numbers, so a volatile one is still caught by the repeat.
* Dates. An ISO date is compared as its day ordinal, so an input echoed back as
  a date is a linear field like any echoed integer.
* Rounding. Money is truncated to cents by legacy systems, so a numeric field
  may differ from its exact line by less than one unit in its last decimal
  place, and extrapolation over a long interval doubles that. Numeric tests
  therefore allow four units in the last place of the value. A real rule change
  moves a value by far more than that over the grid spacing.
* Shared probes. A request already sent (an exhaustive value, or a sample
  another context needed) is reused rather than sent again. Scenario probes are
  never shared: each probe is one complete fresh run, and its signature is the
  sequence of its steps' responses.
* Every exchange becomes an ordinary :class:`~archaeology.trace.Trace`, so
  scenario traces keep their ``scenario`` tag. Each run has a unique number:
  the number the full expansion gives it, and fresh numbers after that for the
  repeat probes.

The limit of the method is the limit of any sampling: two changes that cancel
each other at every probe point (a grace period and a cap whose effects cancel
exactly at the midpoint of a coarse interval) can hide. The unit tests cover
two kinks inside one coarse interval that the neighbours expose.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from itertools import product
from typing import Any

from .infer.catalog import _is_opaque_id, is_iso_date, json_object
from .recorder.exerciser import expand_sweep, expand_workload, request_from_spec
from .scenarios import ScenarioRun, expand_scenarios, run_scenario, vary_values
from .trace import Request, Response, Trace

COARSE_POINTS = 9
TOLERANCE_UNITS = 4
_NUMBER_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")
RequestKey = tuple[str, str, str, str]


# --- observations and signatures -------------------------------------------


@dataclass
class Observation:
    """What one probe returned, before volatile fields are removed.

    ``shape`` has one entry per response (a scenario probe has one per step):
    the status and sorted JSON keys, or ``"text"`` for a body that is not a JSON
    object. ``fields`` maps ``"slot.name"`` to each scalar field's decoded value.
    """

    shape: tuple[str, ...]
    fields: dict[str, Any]


def observe_responses(responses: Sequence[Response]) -> Observation:
    shape: list[str] = []
    fields: dict[str, Any] = {}
    for slot, response in enumerate(responses):
        obj = json_object(response)
        if obj is None:
            shape.append(f"{response.status} text")
            continue
        shape.append(f"{response.status} " + ",".join(sorted(obj)))
        for name, value in obj.items():
            if value is None or isinstance(value, (str, int, float, bool)):
                fields[f"{slot}.{name}"] = value
    return Observation(tuple(shape), fields)


def volatile_fields(first: Observation, again: Observation) -> frozenset[str]:
    """Fields whose values differ between two identical requests."""
    return frozenset(k for k in first.fields.keys() & again.fields.keys() if first.fields[k] != again.fields[k])


@dataclass
class Reading:
    """The comparable signature of one probe: its shape, its text values and its numbers."""

    shape: tuple[str, ...]
    text: dict[str, str]
    numbers: dict[str, Decimal]


def _number(value: Any) -> Decimal | None:
    """The exact decimal of a numeric JSON value, numeric string or ISO date (as its ordinal)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        if _NUMBER_TEXT.fullmatch(value):
            return Decimal(value)
        if is_iso_date(value):
            return Decimal(date.fromisoformat(value).toordinal())
    return None


def classify(observation: Observation, volatile: frozenset[str]) -> Reading:
    """Split an observation into numbers and text, dropping volatile and opaque-id fields."""
    text: dict[str, str] = {}
    numbers: dict[str, Decimal] = {}
    for key, value in observation.fields.items():
        if key in volatile:
            continue
        number = _number(value)
        if number is not None:
            numbers[key] = number
        elif isinstance(value, str) and _is_opaque_id(value):
            continue
        else:
            text[key] = json.dumps(value, sort_keys=True)
    return Reading(observation.shape, text, numbers)


def same_text(x: Reading, y: Reading) -> bool:
    return x.shape == y.shape and x.text == y.text


def _decimals(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _close(actual: Decimal, expected: Fraction) -> bool:
    decimals = _decimals(actual)
    tolerance = Fraction(0) if decimals == 0 else Fraction(TOLERANCE_UNITS, 10**decimals)
    return abs(Fraction(actual) - expected) <= tolerance


def _fits(target: Reading, x_target: int, anchor: Reading, x_anchor: int,
          source: Reading, x_source: int) -> bool:
    """True when every shared number of ``target`` lies on the line through ``anchor`` with the slope of ``source``."""
    for key in target.numbers.keys() & anchor.numbers.keys() & source.numbers.keys():
        a = Fraction(anchor.numbers[key])
        slope = (a - Fraction(source.numbers[key])) / (x_anchor - x_source)
        if not _close(target.numbers[key], a + slope * (x_target - x_anchor)):
            return False
    return True


def _on_chord(mid: Reading, x_mid: int, low: Reading, x_low: int, high: Reading, x_high: int) -> bool:
    """True when every shared number of ``mid`` lies on the chord between ``low`` and ``high``."""
    for key in mid.numbers.keys() & low.numbers.keys() & high.numbers.keys():
        lo, hi = Fraction(low.numbers[key]), Fraction(high.numbers[key])
        expected = lo + (hi - lo) * (x_mid - x_low) / (x_high - x_low)
        if not _close(mid.numbers[key], expected):
            return False
    return True


# --- the search over one axis ----------------------------------------------


def coarse_indices(n: int, count: int = COARSE_POINTS) -> list[int]:
    """``count`` evenly spaced grid indices, both ends included (every index when n <= count)."""
    if n <= count:
        return list(range(n))
    return sorted({round(k * (n - 1) / (count - 1)) for k in range(count)})


class Grid:
    """One axis of one context: grid positions, and the readings of the probes sent so far."""

    def __init__(self, positions: Sequence[int], observe: Callable[[int], Observation],
                 volatile: frozenset[str] = frozenset()) -> None:
        self.positions = list(positions)
        self.readings: dict[int, Reading] = {}
        self._observe = observe
        self._volatile = volatile

    def seed(self, index: int, observation: Observation) -> None:
        self.readings[index] = classify(observation, self._volatile)

    def at(self, index: int) -> Reading:
        if index not in self.readings:
            self.seed(index, self._observe(index))
        return self.readings[index]

    def x(self, index: int) -> int:
        return self.positions[index]

    def before(self, index: int) -> int | None:
        below = [k for k in self.readings if k < index]
        return max(below) if below else None

    def after(self, index: int) -> int | None:
        above = [k for k in self.readings if k > index]
        return min(above) if above else None

    def clean(self, a: int, b: int) -> bool:
        """True when the interval (a, b) shows no change, judged from its endpoints and neighbours.

        The far end must continue the line of the sample before ``a``. Using the
        line after ``b`` as well would call a change exactly at ``a`` clean,
        because that line passes through ``a``. Only at the start of the grid,
        where no sample lies before ``a``, is the line after ``b`` used, and then
        the midpoint must also lie on the chord.
        """
        x, y = self.at(a), self.at(b)
        if not same_text(x, y):
            return False
        p, q = self.before(a), self.after(b)
        if p is not None:
            return _fits(y, self.x(b), x, self.x(a), self.at(p), self.x(p))
        if q is None or not _fits(x, self.x(a), y, self.x(b), self.at(q), self.x(q)):
            return False
        # At the start of the grid only the line after b is known, and it passes
        # through a whenever a change sits exactly at a (a grace period that ends
        # where the line starts), so the midpoint must agree with the chord too.
        return b - a < 2 or self._triple_clean(a, (a + b) // 2, b)

    def changed(self, a: int, b: int) -> bool:
        """True when the adjacent pair (a, b) is itself a change.

        The pair is judged by the line of its left neighbour (or of its right
        neighbour at the start of the grid). Judging by both lines would call a
        continuous kink unchanged: at the kink point the right line also fits.
        """
        x, y = self.at(a), self.at(b)
        if not same_text(x, y):
            return True
        p, q = self.before(a), self.after(b)
        if p is not None:
            return not _fits(y, self.x(b), x, self.x(a), self.at(p), self.x(p))
        if q is not None:
            return not _fits(x, self.x(a), y, self.x(b), self.at(q), self.x(q))
        return False

    def refine(self, a: int, b: int, located: list[tuple[int, int]]) -> None:
        """Locate the changes in an unclean interval (a, b) whose endpoints are known."""
        p, q = self.before(a), self.after(b)
        if p is None or q is None:
            self.bisect(a, b, located)
            return
        a0, b0 = a, b
        while b - a > 1:
            m = (a + b) // 2
            r = self.at(m)
            left_fit = same_text(r, self.at(a)) and _fits(
                r, self.x(m), self.at(a0), self.x(a0), self.at(p), self.x(p))
            right_fit = same_text(r, self.at(b)) and _fits(
                r, self.x(m), self.at(b0), self.x(b0), self.at(q), self.x(q))
            if left_fit:
                a = m
            elif right_fit:
                b = m
            else:
                self.bisect(a, m, located)
                self.bisect(m, b, located)
                return
        located.append((a, b))

    def bisect(self, a: int, b: int, located: list[tuple[int, int]]) -> None:
        """Split (a, b) at its midpoint until every half passes the triple test."""
        if b - a <= 1:
            located.append((a, b))
            return
        m = (a + b) // 2
        if self._triple_clean(a, m, b):
            return
        self.bisect(a, m, located)
        self.bisect(m, b, located)

    def _triple_clean(self, a: int, m: int, b: int) -> bool:
        """True when (a, m, b) shows no change: the midpoint lies on the chord and continues the left line.

        A chord alone cannot see a kink at ``a`` itself: when the line left of
        ``a`` and the chord meet at ``a``, the three points are still collinear.
        The left line is the one ``clean`` uses, so the check only adds the
        continuation the coarse pass already requires.
        """
        x, mid, y = self.at(a), self.at(m), self.at(b)
        if not (same_text(x, mid) and same_text(mid, y)):
            return False
        if not _on_chord(mid, self.x(m), x, self.x(a), y, self.x(b)):
            return False
        p = self.before(a)
        return p is None or _fits(mid, self.x(m), x, self.x(a), self.at(p), self.x(p))


def explore_grid(grid: Grid, count: int = COARSE_POINTS) -> list[tuple[int, int]]:
    """Sample coarsely, then locate every change. Returns the located adjacent index pairs."""
    coarse = coarse_indices(len(grid.positions), count)
    for index in coarse:
        grid.at(index)
    located: list[tuple[int, int]] = []
    for a, b in zip(coarse, coarse[1:]):
        if b - a > 1 and not grid.clean(a, b):
            grid.refine(a, b, located)
    # Bisection leaves next to a change are tested too; keep only the pairs that really change.
    return [(a, b) for a, b in located if grid.changed(a, b)]


def locate(positions: Sequence[int], observe: Callable[[int], Observation]) -> tuple[Grid, list[tuple[int, int]]]:
    """Explore one grid with no volatile fields. Returns the grid (its readings) and the located pairs."""
    grid = Grid(positions, observe)
    return grid, explore_grid(grid)


# --- the workload: what to send, and what to explore -----------------------


def _axis(vary: dict[str, Any]) -> str | None:
    """The first varied key that is an integer range or a date range, if any."""
    for name, spec in vary.items():
        if "range" in spec or "date_range" in spec:
            return name
    return None


def _grid_positions(raw: Sequence[str | None], spec: dict[str, Any]) -> list[int]:
    if "date_range" in spec:
        return [date.fromisoformat(str(v)).toordinal() for v in raw]
    return [int(str(v)) for v in raw]


def _contexts(vary: dict[str, Any], axis: str) -> list[dict[str, str | None]]:
    names = [name for name in vary if name != axis]
    combos = product(*(vary_values(name, vary[name]) for name in names))
    return [dict(zip(names, combo)) for combo in combos]


def _context_label(context: dict[str, str | None]) -> str:
    if not context:
        return "-"
    return ", ".join(f"{k}={'(omitted)' if v is None else v}" for k, v in context.items())


def full_request_count(workload: dict[str, Any]) -> int:
    """How many exchanges ``record`` sends for a workload (computed, not sent)."""
    stateless = len(expand_workload(workload))
    steps = sum(len(run.steps) for run in expand_scenarios(workload))
    return stateless + steps


def _request_key(request: Request) -> RequestKey:
    return (request.method, request.path, request.query_string, request.body)


def _sweep_request(sweep: dict[str, Any], axis: str, value: str | None, context: dict[str, str | None]) -> Request:
    vary: dict[str, Any] = {axis: {"values": [value]}}
    vary.update({name: {"values": [v]} for name, v in context.items()})
    return expand_sweep({**sweep, "vary": vary})[0]


@dataclass
class ExplorationLog:
    """One exploration: an axis in one context, what it spent and what it located."""

    template: str
    axis: str
    context: str
    domain: str
    requests: int
    full: int
    located: list[str] = field(default_factory=list)


@dataclass
class ExploreResult:
    traces: list[Trace]
    explorations: list[ExplorationLog]
    exhaustive: int
    full: int


class _Recorder:
    """Sends requests to the target and turns each exchange into a trace."""

    def __init__(self, target: Callable[[Request], Response], clock: Callable[[], datetime] | None,
                 id_prefix: str) -> None:
        self.target = target
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_prefix = id_prefix
        self.traces: list[Trace] = []
        self.sent: dict[RequestKey, Response] = {}

    def record(self, request: Request, response: Response, scenario: dict[str, Any] | None = None) -> None:
        self.traces.append(
            Trace(f"{self.id_prefix}{len(self.traces) + 1:06d}", self.clock().isoformat(), request, response, scenario)
        )

    def send(self, request: Request) -> Response:
        response = self.target(request)
        self.record(request, response)
        self.sent.setdefault(_request_key(request), response)
        return response


class _Explorer:
    """Runs the exhaustive part of a workload, then explores its adaptive axes."""

    def __init__(self, target: Callable[[Request], Response], workload: dict[str, Any],
                 clock: Callable[[], datetime] | None, id_prefix: str) -> None:
        self.workload = workload
        self.rec = _Recorder(target, clock, id_prefix)
        self.logs: list[ExplorationLog] = []
        self._volatile: dict[tuple[str, ...], frozenset[str]] = {}
        self._runs = expand_scenarios(workload)
        self._run_by_key = {(r.name, tuple(sorted(r.bindings.items()))): r for r in self._runs}
        self._next_run = len(self._runs)

    def run(self) -> ExploreResult:
        for spec in self.workload.get("requests", []):
            self.rec.send(request_from_spec(spec))
        adaptive_sweeps = []
        for sweep in self.workload.get("sweeps", []):
            if _axis(sweep.get("vary", {})) is None:
                for request in expand_sweep(sweep):
                    self.rec.send(request)
            else:
                adaptive_sweeps.append(sweep)
        names = [s["name"] for s in self.workload.get("scenarios", [])]
        if len(set(names)) != len(names):
            raise ValueError("scenario names must be unique to explore a workload")
        adaptive_templates = [s for s in self.workload.get("scenarios", []) if _axis(s.get("vary", {}))]
        adaptive_names = {s["name"] for s in adaptive_templates}
        for run in self._runs:
            if run.name not in adaptive_names:
                run_scenario(run, self.rec.target, on_exchange=self.rec.record)
        exhaustive = len(self.rec.traces)
        for sweep in adaptive_sweeps:
            self._explore_sweep(sweep)
        for template in adaptive_templates:
            self._explore_scenario(template)
        return ExploreResult(self.rec.traces, self.logs, exhaustive, full_request_count(self.workload))

    def _volatility(self, key: tuple[str, ...], first: Observation,
                    again: Callable[[], Observation]) -> frozenset[str]:
        if key not in self._volatile:
            self._volatile[key] = volatile_fields(first, again())
        return self._volatile[key]

    def _explore_sweep(self, sweep: dict[str, Any]) -> None:
        method, path = sweep.get("method", "GET"), sweep["path"]
        axis = _axis(sweep["vary"])
        assert axis is not None
        raw = vary_values(axis, sweep["vary"][axis])
        positions = _grid_positions(raw, sweep["vary"][axis])
        for context in _contexts(sweep["vary"], axis):
            def request_at(i: int, context: dict[str, str | None] = context) -> Request:
                return _sweep_request(sweep, axis, raw[i], context)

            def observe(i: int, request_at: Callable[[int], Request] = request_at) -> Observation:
                request = request_at(i)
                known = self.rec.sent.get(_request_key(request))
                return observe_responses([known if known is not None else self.rec.send(request)])

            before = len(self.rec.traces)
            first = observe(0)
            volatile = self._volatility(
                ("sweep", method, path), first,
                lambda: observe_responses([self.rec.send(request_at(0))]),
            )
            grid = Grid(positions, observe, volatile)
            grid.seed(0, first)
            located = explore_grid(grid)
            self.logs.append(ExplorationLog(
                template=f"{method} {path}", axis=axis, context=_context_label(context),
                domain=f"{raw[0]} to {raw[-1]} ({len(raw)} values)",
                requests=len(self.rec.traces) - before, full=len(raw),
                located=[f"{raw[a]} to {raw[b]}" for a, b in located],
            ))

    def _explore_scenario(self, template: dict[str, Any]) -> None:
        name, steps = template["name"], template["steps"]
        axis = _axis(template["vary"])
        assert axis is not None
        raw = vary_values(axis, template["vary"][axis])
        positions = _grid_positions(raw, template["vary"][axis])
        for context in _contexts(template["vary"], axis):
            def bindings_at(i: int, context: dict[str, str | None] = context) -> dict[str, str]:
                values = {axis: raw[i], **{k: v for k, v in context.items() if v is not None}}
                return {k: str(v) for k, v in values.items()}

            def observe(i: int, bindings_at: Callable[[int], dict[str, str]] = bindings_at) -> Observation:
                bindings = bindings_at(i)
                run = self._run_by_key[(name, tuple(sorted(bindings.items())))]
                return self._observe_run(run)

            before = len(self.rec.traces)
            first = observe(0)
            volatile = self._volatility(
                ("scenario", name), first,
                lambda: self._observe_run(ScenarioRun(name, self._fresh_run(), bindings_at(0), steps)),
            )
            grid = Grid(positions, observe, volatile)
            grid.seed(0, first)
            located = explore_grid(grid)
            self.logs.append(ExplorationLog(
                template=f"scenario {name}", axis=axis, context=_context_label(context),
                domain=f"{raw[0]} to {raw[-1]} ({len(raw)} values)",
                requests=len(self.rec.traces) - before, full=len(raw) * len(steps),
                located=[f"{raw[a]} to {raw[b]}" for a, b in located],
            ))

    def _fresh_run(self) -> int:
        number = self._next_run
        self._next_run += 1
        return number

    def _observe_run(self, run: ScenarioRun) -> Observation:
        exchanges = run_scenario(run, self.rec.target, on_exchange=self.rec.record)
        return observe_responses([response for _, response in exchanges])


def explore_target(
    target: Callable[[Request], Response],
    workload: dict[str, Any],
    clock: Callable[[], datetime] | None = None,
    id_prefix: str = "t-",
) -> ExploreResult:
    """Explore a workload against ``target``. Returns the traces and the exploration log."""
    return _Explorer(target, workload, clock, id_prefix).run()


def render_log(result: ExploreResult) -> str:
    """A markdown exploration log: per axis and context, and the totals against a full sweep."""
    sent = len(result.traces)
    lines = [
        "# Exploration log",
        "",
        "Each row is one axis in one context. `Requests` counts what was sent for it,",
        "`Full sweep` what the same axis costs when every value is sent.",
        "",
        "| Template | Axis | Context | Domain | Requests | Full sweep | Change points |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for log in result.explorations:
        changes = "; ".join(log.located) or "none"
        lines.append(
            f"| {log.template} | {log.axis} | {log.context} | {log.domain} | {log.requests} | {log.full} | {changes} |"
        )
    adaptive = sent - result.exhaustive
    ratio = result.full / sent if sent else float("inf")
    lines += [
        "",
        "## Totals",
        "",
        f"- Exhaustive requests (explicit and value-only sweeps and scenarios): {result.exhaustive}",
        f"- Adaptive requests: {adaptive}",
        f"- Requests sent: {sent}",
        f"- Full recording would send: {result.full}",
        f"- Savings: {ratio:.2f}x fewer requests",
        f"- On the explored axes alone: {adaptive} requests against {result.full - result.exhaustive} "
        f"({(result.full - result.exhaustive) / adaptive if adaptive else float('inf'):.2f}x fewer)",
        "",
    ]
    return "\n".join(lines)
