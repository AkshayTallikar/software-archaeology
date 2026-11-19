"""Stateful replay: identifiers the rebuild issues itself, and scenario probing."""

from __future__ import annotations

import json
import re

from archaeology.cli import main
from archaeology.infer.spec import infer_spec, save_spec, volatile_paths_for
from archaeology.probe import probe_scenarios
from archaeology.recorder import ListSink, exercise
from archaeology.replay import IdentityMap, WsgiTarget, replay
from archaeology.replay.identity import MIN_IDENTIFIER_LENGTH
from archaeology.scenarios import sample_scenarios
from archaeology.trace import Request, Response, Trace, write_traces
from notes_app import make_notes

TEXTS = [
    "short",
    "a note that is comfortably longer than forty characters in total",
    "mid length text",
    "x",
]

WORKLOAD = {
    "requests": [
        {"method": "POST", "path": "/notes", "form": {"text": "  first note  "}},
        {"method": "POST", "path": "/notes", "form": {"text": "  first note  "}},
    ],
    "scenarios": [
        {
            "name": "note-life",
            "vary": {"text": {"values": TEXTS}},
            "steps": [
                {"method": "POST", "path": "/notes", "form": {"text": "{{text}}"}, "capture": {"note": "id"}},
                {"method": "GET", "path": "/notes/{{note}}"},
                {"method": "POST", "path": "/notes/{{note}}/archive"},
                {"method": "GET", "path": "/notes/{{note}}"},
            ],
        }
    ],
}

SCENARIO_STEPS = 4  # steps per note-life run


def _record(app) -> list[Trace]:
    sink = ListSink()
    exercise(app, WORKLOAD, sink)
    return list(sink)


def _recorded():
    """Traces of the reference notes app, and the spec inferred from them."""
    traces = _record(make_notes("n-", 1))
    return traces, infer_spec(traces)


def _resp(body: dict, status: int = 200) -> Response:
    return Response(status=status, headers={"content-type": "application/json"}, body=json.dumps(body))


# --- identity map ----------------------------------------------------------


def test_identity_learns_string_ids_that_differ_and_ignores_the_rest():
    identity = IdentityMap()
    identity.learn(_resp({"id": "n-7f3a91c2", "same": "abcdef"}), _resp({"id": "x-11aa22bb", "same": "abcdef"}),
                   ["id", "same", "missing"])
    assert identity.items() == [("n-7f3a91c2", "x-11aa22bb")]
    assert len(identity) == 1


def test_identity_skips_short_and_non_string_values():
    identity = IdentityMap()
    identity.learn(_resp({"id": "abc", "n": 12345}), _resp({"id": "xyz", "n": 67890}), ["id", "n"])
    assert len(identity) == 0
    assert len("abc") < MIN_IDENTIFIER_LENGTH


def test_identity_ignores_non_json_bodies():
    identity = IdentityMap()
    identity.learn(Response(200, {}, "id=n-7f3a91c2"), Response(200, {}, "id=x-11aa22bb"), ["id"])
    identity.learn(_resp({"id": "n-7f3a91c2"}), Response(200, {}, "n-11aa22bb"), ["id"])
    assert len(identity) == 0


def test_identity_first_mapping_wins():
    identity = IdentityMap()
    identity.learn(_resp({"id": "n-7f3a91c2"}), _resp({"id": "x-first111"}), ["id"])
    identity.learn(_resp({"id": "n-7f3a91c2"}), _resp({"id": "x-second22"}), ["id"])
    assert identity.items() == [("n-7f3a91c2", "x-first111")]
    request = Request.build("GET", "/notes/n-7f3a91c2")
    assert identity.rewrite_request(request).path == "/notes/x-first111"


def test_identity_prefix_overlap_prefers_the_longer_value():
    identity = IdentityMap()
    identity.learn(_resp({"id": "n-7f3a"}), _resp({"id": "x-short1"}), ["id"])
    identity.learn(_resp({"id": "n-7f3a91c2"}), _resp({"id": "x-long2222"}), ["id"])
    assert identity.rewrite_request(Request.build("GET", "/notes/n-7f3a91c2/archive")).path == (
        "/notes/x-long2222/archive"
    )
    assert identity.rewrite_request(Request.build("GET", "/notes/n-7f3a")).path == "/notes/x-short1"


def test_identity_rewrites_url_encoded_occurrences_in_query_and_body():
    identity = IdentityMap()
    identity.learn(_resp({"id": "a b/c"}), _resp({"id": "zz-9"}), ["id"])
    request = Request.build(
        "POST", "/find", query_string="q=a+b%2Fc", body="ref=a%20b%2Fc&raw=a b/c",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    rewritten = identity.rewrite_request(request)
    assert rewritten.query_string == "q=zz-9"
    assert rewritten.body == "ref=zz-9&raw=zz-9"
    assert rewritten.params == {"q": "zz-9", "ref": "zz-9", "raw": "zz-9"}


def test_identity_leaves_unrelated_requests_untouched_and_rewrites_expected_bodies():
    identity = IdentityMap()
    identity.learn(_resp({"id": "n-7f3a91c2"}), _resp({"id": "x-11aa22bb"}), ["id"])
    request = Request.build("GET", "/notes/n-0000aaaa")
    assert identity.rewrite_request(request) is request
    expected = _resp({"id": "n-7f3a91c2", "text": "see n-7f3a91c2"})
    rewritten = identity.rewrite_expected(expected)
    assert json.loads(rewritten.body) == {"id": "x-11aa22bb", "text": "see x-11aa22bb"}
    assert identity.rewrite_expected(_resp({"id": "other"})).body == json.dumps({"id": "other"})


# --- replay with identifiers issued by the rebuild ------------------------


def test_replay_against_a_rebuild_with_its_own_ids_matches_everything():
    traces, spec = _recorded()
    target = WsgiTarget(make_notes("x-", 2))
    report = replay(traces, target, ignore=lambda request: volatile_paths_for(spec, request))
    assert report.total == len(traces)
    assert report.passed(), report.mismatched
    # Every GET and archive of a note names an id the reference issued, so all of them are remapped.
    expected_remapped = sum(1 for t in traces if t.request.path.startswith("/notes/n-"))
    assert expected_remapped == 3 * len(TEXTS)
    assert report.remapped == expected_remapped
    assert all(r.rewritten for r in report.results if r.request_line.startswith("GET /notes/n-"))


def test_recorded_identifiers_cannot_be_replayed_literally():
    traces, _ = _recorded()
    issued: set[str] = set()
    candidate_traces = _record(make_notes("x-", 2))
    for trace in candidate_traces:
        body = trace.response.json()
        if trace.request.path == "/notes" and isinstance(body, dict):
            issued.add(body["id"])
    recorded_in_paths = {
        segment
        for trace in traces
        for segment in trace.request.path.split("/")
        if re.fullmatch(r"n-[0-9a-f]{8}", segment)
    }
    assert recorded_in_paths
    assert recorded_in_paths.isdisjoint(issued)


def test_replay_without_identity_learning_fails_on_recorded_ids():
    traces, _ = _recorded()
    report = replay(traces, WsgiTarget(make_notes("x-", 2)))
    assert not report.passed()
    assert report.remapped == 0
    assert any(r.request_line.startswith("GET /notes/n-") and r.differences for r in report.results)


# --- scenario probing -------------------------------------------------------


def test_scenario_probe_finds_no_divergence_for_a_correct_rebuild():
    traces, spec = _recorded()
    runs = sample_scenarios(WORKLOAD, 40, seed=3)
    report = probe_scenarios(WsgiTarget(make_notes("n-", 1)), WsgiTarget(make_notes("x-", 2)), runs, spec)
    assert report.scenario_steps == 40 * SCENARIO_STEPS
    assert report.total == report.scenario_steps
    assert report.divergences == []


def test_scenario_probe_reports_the_archive_step_of_a_buggy_rebuild():
    traces, spec = _recorded()
    runs = sample_scenarios(WORKLOAD, 40, seed=3)
    report = probe_scenarios(
        WsgiTarget(make_notes("n-", 1)), WsgiTarget(make_notes("x-", 2, bug=True)), runs, spec
    )
    assert report.divergences
    for divergence in report.divergences:
        assert divergence.scenario is not None
        assert divergence.scenario["name"] == "note-life"
        assert divergence.scenario["step"] == 2
        assert divergence.scenario["vars"]["text"] in TEXTS
        assert divergence.request.path.endswith("/archive")
        assert divergence.differences == [f"body.words: expected {len(divergence.scenario['vars']['text'].split())}, got {len(divergence.scenario['vars']['text'].split()) + 1}"]
        assert len(divergence.scenario["vars"]["text"]) > 40


def test_scenario_divergences_render_with_the_scenario_tag():
    from archaeology.probe import render_markdown

    runs = sample_scenarios(WORKLOAD, 40, seed=3)
    _, spec = _recorded()
    report = probe_scenarios(WsgiTarget(make_notes("n-", 1)), WsgiTarget(make_notes("x-", 2, bug=True)), runs, spec)
    markdown = render_markdown(report)
    assert "fresh requests and scenario steps" in markdown
    assert "DIVERGENT" in markdown
    assert "`note-life` step 2 with text=" in markdown
    assert "`POST /notes/{...}/archive`" in markdown


# --- CLI ----------------------------------------------------------------------


def _write_inputs(tmp_path):
    traces, spec = _recorded()
    traces_path = tmp_path / "traces.jsonl"
    spec_path = tmp_path / "spec.json"
    workload_path = tmp_path / "workload.json"
    write_traces(traces_path, traces)
    save_spec(spec, spec_path)
    workload_path.write_text(json.dumps(WORKLOAD), encoding="utf-8")
    return traces_path, spec_path, workload_path


def _probe_args(tmp_path, candidate: str, json_name: str):
    traces_path, spec_path, workload_path = _write_inputs(tmp_path)
    json_path = tmp_path / json_name
    args = [
        "probe",
        "--reference", "notes_app:make_reference",
        "--candidate", candidate,
        "--spec", str(spec_path),
        "--traces", str(traces_path),
        "--count", "40",
        "--workload", str(workload_path),
        "--scenario-count", "25",
        "--json", str(json_path),
    ]
    return args, json_path


def test_cli_probe_with_workload_passes_for_a_correct_rebuild(tmp_path, capsys):
    args, json_path = _probe_args(tmp_path, "notes_app:make_candidate", "probe.json")
    assert main(args) == 0
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    assert summary["scenario_steps"] == 25 * SCENARIO_STEPS
    assert summary["total"] == 40 + 25 * SCENARIO_STEPS
    assert summary["divergences"] == []
    assert "NO DIVERGENCE FOUND" in capsys.readouterr().out


def test_cli_probe_with_workload_fails_for_a_buggy_rebuild(tmp_path, capsys):
    args, json_path = _probe_args(tmp_path, "notes_app:make_buggy_candidate", "probe.json")
    assert main(args) == 1
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    assert summary["divergences"]
    assert all(d["scenario"] and d["scenario"]["step"] == 2 for d in summary["divergences"])
    assert "DIVERGENT" in capsys.readouterr().out


def test_cli_probe_without_workload_ignores_scenario_traces(tmp_path, capsys):
    traces_path, spec_path, _ = _write_inputs(tmp_path)
    json_path = tmp_path / "plain.json"
    code = main([
        "probe", "--reference", "notes_app:make_reference", "--candidate", "notes_app:make_candidate",
        "--spec", str(spec_path), "--traces", str(traces_path), "--count", "30", "--json", str(json_path),
    ])
    assert code == 0
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    assert summary["scenario_steps"] == 0
    assert summary["total"] == 30
