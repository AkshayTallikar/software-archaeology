"""Interleavings: order validation, seeded sampling, tagging, and an end-to-end run on a tiny stateful desk."""

from __future__ import annotations

import json
import random
import re

import pytest

from archaeology.brief import generate_brief
from archaeology.infer import infer_spec
from archaeology.infer.spec import volatile_paths_for
from archaeology.interleave import (
    entry_orders,
    expand_interleavings,
    lane_lengths,
    random_order,
    resolve_lanes,
    run_interleaved,
    sample_interleavings,
    validate_order,
)
from archaeology.probe import probe_interleavings, render_markdown
from archaeology.recorder import ListSink, exercise
from archaeology.replay import WsgiTarget, replay
from archaeology.trace import Request, Response, Trace
from desk_app import make_desk

# A member borrows the one copy and returns it: lane "a" is a template, lane "b" is inline and varies a name.
WORKLOAD = {
    "scenarios": [
        {
            "name": "solo",
            "vary": {"tag": {"values": ["x"]}},
            "steps": [
                {"method": "POST", "path": "/members", "form": {"name": "{{tag}}"}, "capture": {"m": "member_id"}},
                {"method": "POST", "path": "/loans", "form": {"member_id": "{{m}}"}, "capture": {"l": "loan_id"}},
                {"method": "POST", "path": "/loans/{{l}}/return"},
            ],
        }
    ],
    "interleave": [
        {
            "name": "one-copy",
            "lanes": {
                "a": "solo",
                "b": {
                    "steps": [
                        {"method": "POST", "path": "/members", "form": {"name": "{{tag}}"}, "capture": {"m": "member_id"}},
                        {"method": "POST", "path": "/loans", "form": {"member_id": "{{m}}"}, "capture": {"l": "loan_id"}},
                        {"method": "GET", "path": "/copy"},
                        {"method": "POST", "path": "/loans/{{l}}/return"},
                    ],
                    "vary": {"tag": {"values": ["p", "q"]}},
                },
            },
            "vars": {"a": {"tag": "ann"}},
            "orders": [
                ["a0", "a1", "a2", "b0", "b1", "b2", "b3"],
                ["a0", "b0", "a1", "b1", "a2", "b2", "b3"],
                ["b0", "b1", "b2", "a0", "a1", "a2", "b3"],
                ["b0", "a0", "b1", "a1", "b2", "a2", "b3"],
            ],
        }
    ],
}

LANES = {"a": 3, "b": 4}
STEPS_PER_RUN = sum(LANES.values())
RUNS = 4 * 2  # four orders, two values of lane b's tag


def _record(app=None) -> list[Trace]:
    sink = ListSink()
    exercise(app or make_desk("L-", 1), WORKLOAD, sink)
    return list(sink)


def _interleaved(traces: list[Trace]) -> list[Trace]:
    return [t for t in traces if t.scenario is not None and "interleave" in t.scenario]


# --- order validation and sampling ---------------------------------------------


def test_validate_order_accepts_a_valid_order_and_parses_it() -> None:
    assert validate_order(["a0", "b0", "a1"], {"a": 2, "b": 1}) == (("a", 0), ("b", 0), ("a", 1))


@pytest.mark.parametrize(
    "order",
    [
        ["a0", "b0"],  # a step missing
        ["a0", "a0", "b0", "a1"],  # a step repeated
        ["a1", "a0", "b0"],  # a lane's own order broken
        ["c0", "a0", "a1", "b0"],  # unknown lane
        ["a0", "a1", "a2", "b0"],  # step past the end of its lane
        ["a", "a0", "a1", "b0"],  # not a step id
        ["a0", "a1", "b0", "b1"],  # too many steps (the lane has one)
    ],
)
def test_validate_order_rejects_orders_that_break_the_rules(order: list[str]) -> None:
    with pytest.raises(ValueError):
        validate_order(order, {"a": 2, "b": 1})


def test_random_orders_are_valid_and_reproducible_from_a_seed() -> None:
    assert random_order({"a": 3, "b": 2}, random.Random(9)) == random_order({"a": 3, "b": 2}, random.Random(9))
    rng = random.Random(1)
    for _ in range(50):
        order = random_order({"a": 3, "b": 2}, rng)
        validate_order([f"{lane}{index}" for lane, index in order], {"a": 3, "b": 2})


def test_random_orders_reach_every_interleaving_of_two_lanes() -> None:
    rng = random.Random(0)
    seen = {random_order({"a": 2, "b": 2}, rng) for _ in range(600)}
    assert len(seen) == 6  # C(4, 2) interleavings of two lanes of two steps


def test_sampled_orders_are_deterministic_distinct_and_valid() -> None:
    entry = {"name": "x", "sample": {"count": 6, "seed": 3}}
    orders = entry_orders(entry, {"a": 3, "b": 4})
    assert orders == entry_orders(entry, {"a": 3, "b": 4})
    assert len(set(orders)) == len(orders) and orders
    assert entry_orders({"name": "x", "sample": {"count": 6, "seed": 4}}, {"a": 3, "b": 4}) != orders


def test_an_entry_needs_exactly_one_way_to_choose_orders() -> None:
    with pytest.raises(ValueError):
        entry_orders({"name": "x"}, {"a": 1, "b": 1})
    with pytest.raises(ValueError):
        entry_orders({"name": "x", "orders": [["a0", "b0"]], "sample": {"count": 1}}, {"a": 1, "b": 1})


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "x", "lanes": {"a": "solo"}},  # one lane is not an interleaving
        {"name": "x", "lanes": {"a": "solo", "b": "nowhere"}},  # unknown template
        {"name": "x", "lanes": {"a": "solo", "b": "solo"}, "vars": {"c": {"tag": "z"}}},  # vars for no lane
        {"name": "x", "lanes": {"a": "solo", "b2": "solo"}},  # lane name must be letters
        {"name": "x", "lanes": {"a": "solo", "b": {"steps": []}}},  # an empty lane
    ],
)
def test_resolve_lanes_rejects_malformed_entries(entry: dict) -> None:
    with pytest.raises(ValueError):
        resolve_lanes(WORKLOAD, entry)


def test_entry_vars_extend_a_lanes_own_domains() -> None:
    lanes = resolve_lanes(WORKLOAD, WORKLOAD["interleave"][0])
    assert lanes["a"].scenario == "solo"
    assert lanes["a"].vary == {"tag": {"values": ["ann"]}}  # a scalar is one fixed value
    assert lanes["b"].vary == {"tag": {"values": ["p", "q"]}}
    assert lane_lengths(lanes) == LANES


# --- expansion and tagging -----------------------------------------------------


def test_expansion_numbers_lane_runs_uniquely_and_tags_every_step() -> None:
    runs = expand_interleavings(WORKLOAD, first_run=5)
    assert len(runs) == RUNS
    assert [run.id for run in runs] == list(range(RUNS))
    lane_numbers = [lane.run for run in runs for lane in run.lanes.values()]
    assert lane_numbers == list(range(5, 5 + 2 * RUNS))  # two lanes per run, all distinct
    run = runs[0]
    for position, (lane, index) in enumerate(run.order):
        tag = run.tag(position)
        assert tag["interleave"] == {"name": "one-copy", "id": 0, "lane": lane, "position": position}
        assert tag["step"] == index
        assert tag["run"] == run.lanes[lane].run
    assert run.tag(0)["name"] == "solo"
    assert run.tag(0)["vars"] == {"tag": "ann"}


def test_run_interleaved_sends_each_lane_its_own_steps_in_the_global_order() -> None:
    run = expand_interleavings(WORKLOAD)[0]
    sent: list[Request] = []
    counter = iter(range(1000))

    def send(request: Request) -> Response:
        sent.append(request)
        number = next(counter)
        body = {"member_id": f"m-{number}"} if request.path == "/members" else {"loan_id": f"l-{number}"}
        return Response(status=200 if "return" in request.path or request.path.startswith("/copy") else 201,
                        headers={"content-type": "application/json"}, body=json.dumps(body))

    run_interleaved(run, send)
    assert [r.method + " " + r.path for r in sent] == [
        "POST /members", "POST /loans", "POST /loans/l-1/return",  # lane a, its own loan id
        "POST /members", "POST /loans", "GET /copy", "POST /loans/l-4/return",  # lane b, its own loan id
    ]


def test_recording_writes_the_global_order_and_unique_run_numbers() -> None:
    traces = _record()
    assert len(traces) == 3 + RUNS * STEPS_PER_RUN
    interleaved = _interleaved(traces)
    by_id: dict[int, list[Trace]] = {}
    for trace in interleaved:
        by_id.setdefault(trace.scenario["interleave"]["id"], []).append(trace)
    assert sorted(by_id) == list(range(RUNS))
    for run_traces in by_id.values():
        positions = [t.scenario["interleave"]["position"] for t in run_traces]
        assert positions == list(range(STEPS_PER_RUN))
        # The run's traces sit together in the file, in the order they were sent.
        indices = [traces.index(t) for t in run_traces]
        assert indices == list(range(indices[0], indices[0] + STEPS_PER_RUN))
    lane_runs = {(t.scenario["run"], t.scenario["interleave"]["lane"]) for t in interleaved}
    assert len({run for run, _ in lane_runs}) == 2 * RUNS  # every lane run has its own number
    solo = [t for t in traces if t.scenario is not None and "interleave" not in t.scenario]
    assert {t.scenario["run"] for t in solo} == {0}  # the standalone scenario keeps run 0


def test_no_interleave_section_means_no_interleavings() -> None:
    assert expand_interleavings({"scenarios": WORKLOAD["scenarios"]}) == []
    assert sample_interleavings({"scenarios": WORKLOAD["scenarios"]}, 5, seed=1) == []
    assert sample_interleavings(WORKLOAD, 0, seed=1) == []


def test_sampled_interleavings_are_seeded_and_valid() -> None:
    first = sample_interleavings(WORKLOAD, 20, seed=8)
    again = sample_interleavings(WORKLOAD, 20, seed=8)
    assert [(r.order, {k: v.bindings for k, v in r.lanes.items()}) for r in first] == [
        (r.order, {k: v.bindings for k, v in r.lanes.items()}) for r in again
    ]
    for run in first:
        validate_order([f"{lane}{i}" for lane, i in run.order], LANES)
        assert run.lanes["a"].bindings == {"tag": "ann"}
        assert run.lanes["b"].bindings["tag"] in {"p", "q"}


# --- replay, probe and brief on the desk ---------------------------------------


def test_replay_of_recorded_interleavings_matches_a_correct_rebuild() -> None:
    traces = _record()
    spec = infer_spec(traces)
    report = replay(traces, WsgiTarget(make_desk("R-", 2)), ignore=lambda r: volatile_paths_for(spec, r))
    assert report.total == len(traces)
    assert report.passed(), report.mismatched[:2]
    assert report.remapped > 0


def test_replay_catches_a_rebuild_that_frees_a_copy_for_other_members() -> None:
    traces = _record()
    spec = infer_spec(traces)
    report = replay(traces, WsgiTarget(make_desk("R-", 2, bug=True)), ignore=lambda r: volatile_paths_for(spec, r))
    assert not report.passed()
    by_id = {t.id: t for t in traces}
    assert any(by_id[r.trace_id].scenario and "interleave" in by_id[r.trace_id].scenario for r in report.mismatched)


def test_probe_of_fresh_interleavings_is_clean_for_a_correct_rebuild() -> None:
    spec = infer_spec(_record())
    runs = sample_interleavings(WORKLOAD, 30, seed=4)
    report = probe_interleavings(WsgiTarget(make_desk("L-", 1)), WsgiTarget(make_desk("R-", 2)), runs, spec)
    assert report.total == 30 * STEPS_PER_RUN
    assert report.scenario_steps == report.total
    assert report.divergences == []


def test_probe_reports_each_divergence_with_its_interleaving_order() -> None:
    spec = infer_spec(_record())
    runs = sample_interleavings(WORKLOAD, 30, seed=4)
    report = probe_interleavings(WsgiTarget(make_desk("L-", 1)), WsgiTarget(make_desk("R-", 2, bug=True)), runs, spec)
    assert report.divergences
    orders = {run.order_label() for run in runs}
    for divergence in report.divergences:
        assert divergence.order in orders
        assert divergence.scenario is not None and divergence.scenario["interleave"]["name"] == "one-copy"
        assert divergence.path_template is not None
    markdown = render_markdown(report)
    assert "in interleaving `" in markdown


def test_brief_lists_each_interleaving_and_the_responses_that_differ_between_orders() -> None:
    traces = _record()
    spec = infer_spec(traces)
    brief = generate_brief(spec, traces, system_name="Desk")
    section = brief.split("## Interactions between clients", 1)[1]
    section = re.split(r"\n## ", section, maxsplit=1)[0]
    assert "### Interleaving `one-copy`" in section
    assert "Recorded orders: 4." in section
    for order in ("a0 a1 a2 b0 b1 b2 b3", "b0 a0 b1 a1 b2 a2 b3"):
        assert f"- Order `{order}`" in section
    # Lane b's loan on a copy lane a holds is refused in one order and granted in another.
    assert "order `a0 a1 a2 b0 b1 b2 b3`: 201" in section
    assert "409 BOOK_UNAVAILABLE" in section
