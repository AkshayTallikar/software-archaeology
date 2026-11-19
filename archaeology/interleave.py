"""Interleavings: several clients' scenarios run in one shared global order.

A scenario is one client at a time. Behaviour that only appears between
clients (two members after one copy, a return racing a borrow) needs the
steps of several scenarios interleaved in one order. A workload's top-level
``interleave`` section lists such workloads::

    {
      "name": "same-copy",
      "lanes": {
        "a": "borrow-one",
        "b": {"steps": [{"method": "POST", "path": "/members", "form": {"name": "Ben"}, "capture": {"m": "member_id"}},
                        {"method": "POST", "path": "/loans", "form": {"member_id": "{{m}}", "isbn": "{{isbn}}"}}],
              "vary": {"isbn": {"values": ["9780000000316"]}}}
      },
      "vars": {"a": {"isbn": "9780000000316"}},
      "orders": [["a0", "b0", "a1", "b1"], ["b0", "b1", "a0", "a1"]]
    }

* ``lanes`` names two or more lanes. A lane is either the name of a scenario
  template in ``scenarios`` or an inline ``{"steps": [...], "vary": {...}}``.
  Lane names are letters and underscores only.
* ``vars`` (optional) gives per-lane variable values that extend the lane's
  own ``vary``: a scalar is one fixed value (``null`` omits the parameter), a
  ``{"values"|"range"|"date_range": ...}`` spec is a domain, as in ``vary``.
  Every combination of a lane's domains is one run.
* Each lane keeps its own variables and captures. A step is named
  ``<lane><index>``, its index in the lane's own step list (``b1`` is the
  second step of lane ``b``).
* ``orders`` lists global orders. Each order names every step of every lane
  exactly once and keeps each lane's own step order. Alternatively
  ``"sample": {"count": N, "seed": S}`` draws up to N distinct valid orders
  from a seeded generator, so the same seed always records the same orders.

Recording runs the steps of each order in turn against the target. Every
exchange carries the usual ``scenario`` tag of its lane's run (``name``,
``run``, ``step``, ``vars``), with an extra
``interleave: {"name", "id", "lane", "position"}`` that recovers the global
order: ``id`` numbers the interleaved runs of the workload, ``position`` is
the step's place in the order. Lane run numbers stay unique across the
workload's scenarios and interleavings. The trace file keeps the global
order, so replay (which sends traces in file order) replays the interleaving
as recorded; the replay identity map is shared by every lane, so an
identifier issued to one lane is translated wherever a later request uses it.

Probing draws fresh runs with :func:`sample_interleavings`: a random entry, a
uniformly random valid order and values sampled from each lane's domains.
"""

from __future__ import annotations

import itertools
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from .scenarios import ScenarioRun, apply_captures, render_step, sample_value, vary_values
from .trace import Request, Response

Send = Callable[[Request], Response]
OnExchange = Callable[[Request, Response, dict[str, Any]], None]

_STEP_ID = re.compile(r"([A-Za-z_]+)(\d+)")
_LANE_NAME = re.compile(r"[A-Za-z_]+")

Order = tuple[tuple[str, int], ...]  # (lane, step index) in global order


@dataclass(frozen=True)
class Lane:
    """One lane of an interleaving: its scenario tag name, steps and variable domains."""

    name: str
    scenario: str
    steps: tuple[dict[str, Any], ...]
    vary: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterleavedRun:
    """One global order of an interleaving, with each lane's own run (bindings, steps, run number)."""

    name: str
    id: int
    lanes: dict[str, ScenarioRun]
    order: Order

    def tag(self, position: int) -> dict[str, Any]:
        """The ``Trace.scenario`` tag of the step at ``position`` in the global order."""
        lane, index = self.order[position]
        tag = self.lanes[lane].tag(index)
        tag["interleave"] = {"name": self.name, "id": self.id, "lane": lane, "position": position}
        return tag

    def order_label(self) -> str:
        """The order as step ids: ``a0 b0 a1 b1``."""
        return " ".join(order_id(lane, index) for lane, index in self.order)


# --- lanes and orders ----------------------------------------------------------


def order_id(lane: str, index: int) -> str:
    return f"{lane}{index}"


def lane_lengths(lanes: Mapping[str, Lane]) -> dict[str, int]:
    return {name: len(lane.steps) for name, lane in lanes.items()}


def resolve_lanes(workload: dict[str, Any], entry: dict[str, Any]) -> dict[str, Lane]:
    """The entry's lanes, with its ``vars`` merged into each lane's variable domains."""
    name = entry.get("name", "?")
    refs = entry.get("lanes")
    if not isinstance(refs, dict) or len(refs) < 2:
        raise ValueError(f"interleave {name!r} needs a 'lanes' object naming two or more lanes")
    templates = {t["name"]: t for t in workload.get("scenarios", [])}
    overrides = entry.get("vars", {})
    unknown = set(overrides) - set(refs)
    if unknown:
        raise ValueError(f"interleave {name!r} sets vars for unknown lanes {sorted(unknown)}")
    lanes: dict[str, Lane] = {}
    for lane_name, ref in refs.items():
        if not _LANE_NAME.fullmatch(lane_name):
            raise ValueError(f"interleave {name!r}: lane name {lane_name!r} must be letters or underscores")
        if isinstance(ref, str):
            if ref not in templates:
                raise ValueError(f"interleave {name!r}: lane {lane_name!r} names unknown scenario {ref!r}")
            scenario, steps, vary = ref, templates[ref]["steps"], dict(templates[ref].get("vary", {}))
        elif isinstance(ref, dict) and "steps" in ref:
            scenario, steps, vary = f"{name}/{lane_name}", ref["steps"], dict(ref.get("vary", {}))
        else:
            raise ValueError(f"interleave {name!r}: lane {lane_name!r} must name a scenario or give 'steps'")
        if not steps:
            raise ValueError(f"interleave {name!r}: lane {lane_name!r} has no steps")
        for var, value in overrides.get(lane_name, {}).items():
            vary[var] = value if isinstance(value, dict) else {"values": [value]}
        lanes[lane_name] = Lane(lane_name, scenario, tuple(steps), vary)
    return lanes


def validate_order(order: Sequence[str], lengths: Mapping[str, int]) -> Order:
    """Parse an order of step ids, checking that it names every step of every lane exactly once, in each lane's own order.

    Raises ``ValueError`` describing the first problem found.
    """
    parsed: list[tuple[str, int]] = []
    for step_id in order:
        match = _STEP_ID.fullmatch(step_id)
        if match is None or match.group(1) not in lengths:
            raise ValueError(f"order names unknown step {step_id!r}")
        lane, index = match.group(1), int(match.group(2))
        if index >= lengths[lane]:
            raise ValueError(f"order names step {step_id!r}, but lane {lane!r} has {lengths[lane]} steps")
        parsed.append((lane, index))
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"order repeats a step: {' '.join(order)}")
    expected = sum(lengths.values())
    if len(parsed) != expected:
        raise ValueError(f"order has {len(parsed)} steps, the lanes have {expected}")
    for lane, length in lengths.items():
        seen = [index for name, index in parsed if name == lane]
        if seen != list(range(length)):
            raise ValueError(f"order does not keep lane {lane!r}'s steps in their own order")
    return tuple(parsed)


def random_order(lengths: Mapping[str, int], rng: random.Random) -> Order:
    """A valid order drawn uniformly from all interleavings of the lanes.

    Each step picks the next lane with probability proportional to its remaining steps, which is
    uniform over the interleavings.
    """
    remaining = dict(lengths)
    cursor = {lane: 0 for lane in lengths}
    order: list[tuple[str, int]] = []
    while any(remaining.values()):
        choices = [lane for lane, left in remaining.items() for _ in range(left)]
        lane = rng.choice(choices)
        order.append((lane, cursor[lane]))
        cursor[lane] += 1
        remaining[lane] -= 1
    return tuple(order)


def entry_orders(entry: dict[str, Any], lengths: Mapping[str, int]) -> list[Order]:
    """The orders a workload entry records: its explicit ``orders``, or ``sample``'s seeded draw."""
    has_orders, has_sample = "orders" in entry, "sample" in entry
    if has_orders == has_sample:
        raise ValueError(f"interleave {entry.get('name', '?')!r} needs exactly one of 'orders' or 'sample'")
    if has_orders:
        return [validate_order(order, lengths) for order in entry["orders"]]
    count, seed = int(entry["sample"]["count"]), int(entry["sample"].get("seed", 0))
    rng = random.Random(seed)
    orders: list[Order] = []
    for _ in range(count):
        order = random_order(lengths, rng)
        if order not in orders:
            orders.append(order)
    return orders


# --- expansion -----------------------------------------------------------------


def _lane_combinations(lane: Lane) -> list[tuple[dict[str, str], frozenset[str]]]:
    """Every combination of a lane's variable domains as (bindings, omitted names)."""
    names = list(lane.vary)
    combos = []
    for combo in itertools.product(*(vary_values(n, lane.vary[n]) for n in names)):
        bindings = {n: v for n, v in zip(names, combo) if v is not None}
        omitted = frozenset(n for n, v in zip(names, combo) if v is None)
        combos.append((bindings, omitted))
    return combos


def expand_interleavings(workload: dict[str, Any], first_run: int = 0) -> list[InterleavedRun]:
    """One run per entry, per recorded order, per combination of the lanes' variable domains.

    Lane run numbers start at ``first_run`` and are unique; interleaved run ids count from 0.
    """
    runs: list[InterleavedRun] = []
    run_number = first_run
    for entry in workload.get("interleave", []):
        lanes = resolve_lanes(workload, entry)
        lengths = lane_lengths(lanes)
        combos_per_lane = [_lane_combinations(lanes[name]) for name in lanes]
        for order in entry_orders(entry, lengths):
            for combo in itertools.product(*combos_per_lane):
                lane_runs: dict[str, ScenarioRun] = {}
                for name, (bindings, omitted) in zip(lanes, combo):
                    lane = lanes[name]
                    lane_runs[name] = ScenarioRun(lane.scenario, run_number, bindings, list(lane.steps), omitted)
                    run_number += 1
                runs.append(InterleavedRun(entry["name"], len(runs), lane_runs, order))
    return runs


def interleaving_request_count(workload: dict[str, Any]) -> int:
    """How many requests the workload's ``interleave`` section sends when recorded in full."""
    return sum(len(run.order) for run in expand_interleavings(workload))


def sample_interleavings(workload: dict[str, Any], count: int, seed: int = 0, first_run: int = 0) -> list[InterleavedRun]:
    """``count`` fresh interleaved runs: a random entry, a random valid order, values drawn from each lane's domains."""
    entries = workload.get("interleave", [])
    if not entries or count <= 0:
        return []
    resolved = [(entry, resolve_lanes(workload, entry)) for entry in entries]
    rng = random.Random(seed)
    runs: list[InterleavedRun] = []
    run_number = first_run
    for offset in range(count):
        entry, lanes = rng.choice(resolved)
        order = random_order(lane_lengths(lanes), rng)
        lane_runs: dict[str, ScenarioRun] = {}
        for name, lane in lanes.items():
            bindings: dict[str, str] = {}
            omitted: set[str] = set()
            for var, spec in lane.vary.items():
                value = sample_value(var, spec, rng)
                if value is None:
                    omitted.add(var)
                else:
                    bindings[var] = value
            lane_runs[name] = ScenarioRun(lane.scenario, run_number, bindings, list(lane.steps), frozenset(omitted))
            run_number += 1
        runs.append(InterleavedRun(entry["name"], offset, lane_runs, order))
    return runs


# --- rendering and running -----------------------------------------------------


def run_interleaved(run: InterleavedRun, send: Send, on_exchange: OnExchange | None = None) -> list[tuple[Request, Response]]:
    """Run the steps of ``run`` in its global order against ``send``, each lane with its own variables and captures."""
    variables = {name: dict(lane.bindings) for name, lane in run.lanes.items()}
    exchanges: list[tuple[Request, Response]] = []
    for position, (lane_name, index) in enumerate(run.order):
        lane = run.lanes[lane_name]
        step = lane.steps[index]
        request = render_step(step, variables[lane_name], lane.omitted)
        response = send(request)
        apply_captures(step, response, variables[lane_name])
        exchanges.append((request, response))
        if on_exchange is not None:
            on_exchange(request, response, run.tag(position))
    return exchanges
