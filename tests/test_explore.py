"""Explore: adaptive sampling finds the behaviour that the full recording shows, with fewer requests.

The bundled systems are the judges. For each one, the findings that
``detect_boundaries`` reports on the explored traces must equal the findings on
the full recording, after both are reduced to the same normalized form. The
request budgets are the acceptance numbers: billing at most a fifth of the full
recording, library at most two fifths.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from archaeology.explore import Grid, Observation, explore_grid, explore_target, locate, render_log
from archaeology.infer.boundaries import detect_boundaries
from archaeology.recorder import ListSink, exercise, exercise_target, load_workload
from archaeology.replay import ProcessTarget
from archaeology.wsgi import call_wsgi
from legacy_app.billing import make_app as make_billing_app

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "legacy_library" / "server.js"
BILLING_WORKLOAD = "legacy_app/workload.json"
LIBRARY_WORKLOAD = "legacy_library/workload.json"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def normalized(findings: list[dict[str, Any]]) -> list[str]:
    """Reduce boundary findings to the facts that matter, so explored and full findings compare directly."""
    out = []
    for f in findings:
        segments = f.get("segments")
        payload = segments if segments is not None else [f.get("echo_until"), f.get("then")]
        out.append(json.dumps({
            "kind": f["kind"], "endpoint": f["endpoint"], "scenario": f.get("scenario"),
            "steps": f.get("steps", f.get("step")), "param": f["param"], "field": f["field"],
            "context": f["context"], "payload": payload, "contexts": f["contexts"],
        }, sort_keys=True))
    return sorted(out)


# --- synthetic axes: the bisection itself ----------------------------------


def _observe_fn(function, positions):
    """An observe callable over grid indices, for a function of the grid value."""
    calls: list[int] = []

    def observe(index: int) -> Observation:
        calls.append(index)
        return Observation(("200 object",), {"0.value": function(positions[index])})

    return observe, calls


def _located_values(positions, pairs):
    return [(positions[a], positions[b]) for a, b in pairs]


def test_step_is_located_at_its_adjacent_pair():
    positions = list(range(0, 100))
    observe, calls = _observe_fn(lambda x: 0 if x < 40 else 1, positions)
    grid, pairs = locate(positions, observe)
    assert _located_values(positions, pairs) == [(39, 40)]
    assert len(set(calls)) < len(positions) // 4


def test_grace_then_linear_then_cap_locates_both_kinks_and_the_cap():
    positions = list(range(0, 121))
    # Nothing for 20 days, then 25 cents per day, capped at 10.00. Integer cents
    # compare exactly; a value such as 10.0 would allow a tolerance of 0.4.
    function = lambda x: 0 if x <= 20 else min(1000, (x - 20) * 25)  # noqa: E731
    observe, calls = _observe_fn(function, positions)
    _, pairs = locate(positions, observe)
    found = _located_values(positions, pairs)
    assert (20, 21) in found
    assert (60, 61) in found
    assert len(set(calls)) < len(positions) // 3


def test_date_axis_locates_the_day_a_lateness_starts():
    start = date(2026, 1, 10).toordinal()
    positions = list(range(start, start + 60))
    first_late = date(2026, 1, 25).toordinal()
    observe, _ = _observe_fn(lambda day: max(0, day - first_late), positions)
    _, pairs = locate(positions, observe)
    assert [(date.fromordinal(positions[a]), date.fromordinal(positions[b])) for a, b in pairs] == [
        (date(2026, 1, 25), date(2026, 1, 26)),
    ]


def test_two_kinks_inside_one_coarse_interval_are_both_located():
    positions = list(range(0, 81))  # coarse points every ten values
    # A slope change at 13|14 and a jump at 17|18, both between coarse points 10 and 20.
    function = lambda x: max(0, x - 13) + (5 if x >= 18 else 0)  # noqa: E731
    observe, _ = _observe_fn(function, positions)
    _, pairs = locate(positions, observe)
    found = _located_values(positions, pairs)
    assert (13, 14) in found
    assert (17, 18) in found


def test_grid_seed_is_reused_and_not_resent():
    positions = list(range(0, 30))
    calls: list[int] = []

    def observe(index: int) -> Observation:
        calls.append(index)
        return Observation(("200 object",), {"0.value": 0})

    grid = Grid(positions, observe)
    explore_grid(grid)
    assert len(calls) == len(set(calls))


# --- the bundled systems: findings must match the full recording -----------


@pytest.fixture(scope="module")
def billing_full() -> list:
    sink = ListSink()
    exercise(make_billing_app(), load_workload(BILLING_WORKLOAD), sink)
    return list(sink)


@pytest.fixture(scope="module")
def billing_explored():
    app = make_billing_app()
    return explore_target(lambda request: call_wsgi(app, request), load_workload(BILLING_WORKLOAD))


def test_billing_findings_match_the_full_recording(billing_full, billing_explored):
    assert normalized(detect_boundaries(billing_explored.traces)) == normalized(detect_boundaries(billing_full))


def test_billing_sends_at_most_a_fifth_of_the_full_recording(billing_full, billing_explored):
    assert len(billing_explored.traces) * 5 <= len(billing_full)


def test_billing_exploration_log_accounts_for_every_request(billing_explored):
    adaptive = len(billing_explored.traces) - billing_explored.exhaustive
    assert adaptive == sum(log.requests for log in billing_explored.explorations)
    assert "Requests sent" in render_log(billing_explored)


@pytest.fixture(scope="module")
def library_full() -> list:
    sink = ListSink()
    with ProcessTarget(["node", str(SERVER)]) as target:
        exercise_target(target, load_workload(LIBRARY_WORKLOAD), sink)
    return list(sink)


@pytest.fixture(scope="module")
def library_explored():
    with ProcessTarget(["node", str(SERVER)]) as target:
        return explore_target(target, load_workload(LIBRARY_WORKLOAD))


@needs_node
def test_library_findings_match_the_full_recording(library_full, library_explored):
    assert normalized(detect_boundaries(library_explored.traces)) == normalized(detect_boundaries(library_full))


@needs_node
def test_library_explores_its_axes_with_at_least_two_and_a_half_times_fewer_requests(library_full, library_explored):
    # Value-list scenarios (check order, error codes) are sent exhaustively by both
    # recorders, so the saving is measured on the axes that explore actually explores.
    explored = len(library_explored.traces) - library_explored.exhaustive
    swept = len(library_full) - library_explored.exhaustive
    assert library_explored.full == len(library_full)
    assert explored * 2.5 <= swept


@needs_node
def test_library_scenario_runs_keep_their_numbers_and_step_order(library_explored):
    steps_by_run: dict[int, set[int]] = {}
    names_by_run: dict[int, str] = {}
    for trace in library_explored.traces:
        if trace.scenario is None:
            continue
        run, name = trace.scenario["run"], trace.scenario["name"]
        steps_by_run.setdefault(run, set()).add(trace.scenario["step"])
        assert names_by_run.setdefault(run, name) == name, f"run {run} is used by two scenarios"
    for run, steps in steps_by_run.items():
        assert steps == set(range(len(steps))), f"run {run} has steps {sorted(steps)}"
    ids = [trace.id for trace in library_explored.traces]
    assert len(ids) == len(set(ids))
