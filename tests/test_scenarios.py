import json
import random
import sys

import pytest

from archaeology.recorder import ListSink, exercise, exercise_target, expand_workload
from archaeology.replay import ProcessTarget, WsgiTarget, load_target
from archaeology.scenarios import (
    ScenarioRun,
    expand_scenarios,
    json_path_get,
    render_step,
    run_scenario,
    sample_scenarios,
    substitute,
    vary_values,
)
from archaeology.trace import Response, Trace
from legacy_app.billing import make_app

INVOICE_SCENARIO = {
    "scenarios": [
        {
            "name": "invoice-then-quote",
            "vary": {"qty": {"values": [1, 2]}, "coupon": {"values": [None, "SAVE10"]}},
            "steps": [
                {"method": "POST", "path": "/invoice",
                 "form": {"customer_id": "1001", "sku": "WIDGET", "qty": "{{qty}}", "coupon": "{{coupon}}"},
                 "capture": {"total": "total", "inv": "invoice_id"}},
                {"method": "GET", "path": "/quote", "query": {"sku": "WIDGET", "qty": "{{qty}}", "ref": "{{inv}}"}},
            ],
        }
    ]
}


def test_vary_values_cover_values_ranges_and_dates():
    assert vary_values("a", {"values": [1, None, "x"]}) == ["1", None, "x"]
    assert vary_values("a", {"range": [1, 7, 3]}) == ["1", "4", "7"]
    assert vary_values("d", {"date_range": ["2026-02-27", "2026-03-02", 1]}) == [
        "2026-02-27", "2026-02-28", "2026-03-01", "2026-03-02",
    ]
    assert vary_values("d", {"date_range": ["2026-01-01", "2026-01-10", 4]}) == [
        "2026-01-01", "2026-01-05", "2026-01-09",
    ]


def test_sweeps_accept_date_ranges():
    workload = {"sweeps": [{"path": "/x", "vary": {"on": {"date_range": ["2026-01-30", "2026-02-01"]}}}]}
    assert [r.params["on"] for r in expand_workload(workload)] == ["2026-01-30", "2026-01-31", "2026-02-01"]


def test_expansion_numbers_runs_and_binds_variables():
    runs = expand_scenarios(INVOICE_SCENARIO, first_run=10)
    assert [r.run for r in runs] == [10, 11, 12, 13]
    assert [r.bindings for r in runs] == [
        {"qty": "1"}, {"qty": "1", "coupon": "SAVE10"}, {"qty": "2"}, {"qty": "2", "coupon": "SAVE10"},
    ]
    assert runs[0].tag(1) == {"name": "invoice-then-quote", "run": 10, "step": 1}


def test_substitute_leaves_unbound_placeholders_visible():
    assert substitute("/loans/{{ loan }}/return?x={{y}}", {"loan": "L-1"}) == "/loans/L-1/return?x={{y}}"


def test_render_step_substitutes_path_query_and_form():
    step = {"method": "POST", "path": "/m/{{id}}", "query": {"q": "{{id}}"}, "form": {"f": "a{{id}}"}}
    request = render_step(step, {"id": "m_1f"})
    assert request.path == "/m/m_1f"
    assert request.query_string == "q=m_1f"
    assert request.params == {"q": "m_1f", "f": "am_1f"}


def test_json_path_get():
    body = {"items": [{"isbn": "1"}, {"isbn": "2"}], "n": 0}
    assert json_path_get(body, "items.1.isbn") == "2"
    assert json_path_get(body, "n") == 0
    assert json_path_get(body, "items.9.isbn") is None
    assert json_path_get(None, "x") is None


def test_run_scenario_captures_and_reuses_values():
    target = WsgiTarget(make_app())
    run = ScenarioRun("s", 0, {"qty": "3"}, INVOICE_SCENARIO["scenarios"][0]["steps"])
    tags = []
    exchanges = run_scenario(run, target, on_exchange=lambda req, resp, tag: tags.append(tag))
    invoice = exchanges[0][1].json()
    assert exchanges[1][0].params["ref"] == invoice["invoice_id"]
    # an unbound coupon is sent as the literal placeholder, so the difference stays visible
    assert exchanges[0][0].params["coupon"] == "{{coupon}}"
    assert tags == [{"name": "s", "run": 0, "step": 0}, {"name": "s", "run": 0, "step": 1}]


def test_exercise_tags_scenario_traces_and_numbers_after_plain_requests():
    workload = dict(INVOICE_SCENARIO, requests=[{"path": "/quote", "query": {"sku": "WIDGET", "qty": "1"}}])
    sink = ListSink()
    assert exercise(make_app(), workload, sink) == 1 + 4 * 2
    traces = list(sink)
    assert traces[0].scenario is None
    assert [t.id for t in traces[:3]] == ["t-000001", "t-000002", "t-000003"]
    assert traces[1].scenario == {"name": "invoice-then-quote", "run": 0, "step": 0}
    assert traces[-1].scenario == {"name": "invoice-then-quote", "run": 3, "step": 1}
    round_tripped = Trace.from_dict(json.loads(json.dumps(traces[1].to_dict())))
    assert round_tripped == traces[1]


def test_old_traces_without_a_scenario_field_still_load():
    data = Trace("t-1", "", render_step({"path": "/x"}, {}), Response(200)).to_dict()
    del data["scenario"]
    assert Trace.from_dict(data).scenario is None


def test_sampling_is_seeded_and_draws_from_the_whole_domain():
    workload = {"scenarios": [{"name": "a", "vary": {
        "n": {"range": [1, 1000, 100]}, "d": {"date_range": ["2026-01-01", "2026-12-31", 30]},
    }, "steps": []}, {"name": "b", "steps": []}]}
    first = sample_scenarios(workload, 200, seed=4, first_run=5)
    again = sample_scenarios(workload, 200, seed=4, first_run=5)
    assert [(r.name, r.bindings) for r in first] == [(r.name, r.bindings) for r in again]
    assert first[0].run == 5 and first[-1].run == 204
    assert {r.name for r in first} == {"a", "b"}
    a_runs = [r for r in first if r.name == "a"]
    grid = set(vary_values("n", {"range": [1, 1000, 100]}))
    assert any(r.bindings["n"] not in grid for r in a_runs)
    assert all(1 <= int(r.bindings["n"]) <= 1000 for r in a_runs)
    assert all("2026-01-01" <= r.bindings["d"] <= "2026-12-31" for r in a_runs)
    assert sample_scenarios({}, 5) == []


def test_process_target_runs_a_system_over_http():
    command = [sys.executable, "-m", "archaeology.serve", "legacy_app.billing:make_app"]
    with ProcessTarget(command) as target:
        sink = ListSink()
        count = exercise_target(target, INVOICE_SCENARIO, sink)
        assert count == 8
        in_process = ListSink()
        exercise(make_app(), INVOICE_SCENARIO, in_process)
        # identical apart from generated invoice ids and timestamps
        assert [t.response.status for t in sink] == [t.response.status for t in in_process]
        assert [t.request.params.keys() for t in sink] == [t.request.params.keys() for t in in_process]
        assert sink[1].response.json()["total"] == in_process[1].response.json()["total"]


def test_cmd_spec_builds_a_fresh_process_each_time():
    spec = f"cmd:{sys.executable} -m archaeology.serve legacy_app.billing:make_app"
    first, second = load_target(spec), load_target(spec)
    try:
        assert isinstance(first, ProcessTarget) and first.port != second.port
        quote = render_step({"path": "/quote", "query": {"sku": "WIDGET", "qty": "2"}}, {})
        assert first(quote).json() == second(quote).json()
    finally:
        first.close()
        second.close()
        first.close()  # safe twice


def test_process_target_reports_a_command_that_never_listens():
    with pytest.raises(RuntimeError, match="could not start"):
        ProcessTarget([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=2)
