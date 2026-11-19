"""Tests for the recorder stage: middleware, sinks, sweeps and the exerciser."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from archaeology.recorder import (
    JsonlSink,
    ListSink,
    RecordingMiddleware,
    expand_sweep,
    expand_workload,
    exercise,
    load_workload,
    record,
    request_from_spec,
)
from archaeology.trace import Request, read_traces
from archaeology.wsgi import call_wsgi


def echo_app(environ, start_response):
    """Echo method, path, query and body as JSON; /missing returns a text 404."""
    length = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(length).decode("utf-8") if length else ""
    if environ["PATH_INFO"] == "/missing":
        data = b"no such page"
        start_response(
            "404 Not Found",
            [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(data)))],
        )
        return [data]
    payload = {
        "method": environ["REQUEST_METHOD"],
        "path": environ["PATH_INFO"],
        "query": dict(parse_qsl(environ["QUERY_STRING"], keep_blank_values=True)),
        "body": body,
    }
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    start_response(
        "200 OK",
        [("Content-Type", "application/json"), ("Content-Length", str(len(data)))],
    )
    return [data]


FIXED = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)


def fixed_clock():
    return FIXED


def test_middleware_records_query_and_form_params():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    response = call_wsgi(
        app,
        request_from_spec({"method": "POST", "path": "/invoice", "query": {"ref": "A1"}, "form": {"customer_id": "1001", "sku": "WIDGET"}}),
    )
    assert response.status == 200
    trace = sink[0]
    assert trace.request.method == "POST"
    assert trace.request.path == "/invoice"
    assert trace.request.params == {"ref": "A1", "customer_id": "1001", "sku": "WIDGET"}
    assert trace.request.body == "customer_id=1001&sku=WIDGET"
    assert trace.request.headers["content-type"] == "application/x-www-form-urlencoded"


def test_body_is_still_readable_by_wrapped_app():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    response = call_wsgi(app, request_from_spec({"method": "POST", "path": "/echo", "form": {"a": "b c"}}))
    payload = json.loads(response.body)
    assert payload["body"] == "a=b+c"
    assert payload["method"] == "POST"


def test_status_headers_and_body_captured():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    response = call_wsgi(app, request_from_spec({"path": "/missing"}))
    assert response.status == 404
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.body == "no such page"
    trace = sink[0]
    assert trace.response.status == 404
    assert trace.response.headers["content-type"] == "text/plain; charset=utf-8"
    assert trace.response.body == "no such page"


def test_sequential_ids_and_count():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock, id_prefix="x-")
    for _ in range(3):
        call_wsgi(app, request_from_spec({"path": "/quote"}))
    assert [t.id for t in sink] == ["x-000001", "x-000002", "x-000003"]
    assert app.count == 3


def test_default_id_prefix():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    call_wsgi(app, request_from_spec({"path": "/quote"}))
    assert sink[0].id == "t-000001"


def test_deterministic_ts_with_injected_clock():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    call_wsgi(app, request_from_spec({"path": "/quote"}))
    call_wsgi(app, request_from_spec({"path": "/quote"}))
    assert [t.ts for t in sink] == [FIXED.isoformat(), FIXED.isoformat()]


def test_default_clock_is_utc_aware():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink)
    call_wsgi(app, request_from_spec({"path": "/quote"}))
    assert datetime.fromisoformat(sink[0].ts).utcoffset().total_seconds() == 0


def test_missing_or_empty_content_length_gives_empty_body():
    sink = ListSink()
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/quote",
        "QUERY_STRING": "sku=WIDGET",
        "wsgi.input": io.BytesIO(b""),
    }
    app(environ, lambda status, headers, exc_info=None: None)
    environ_empty = dict(environ, CONTENT_LENGTH="", **{"wsgi.input": io.BytesIO(b"")})
    app(environ_empty, lambda status, headers, exc_info=None: None)
    assert sink[0].request.body == ""
    assert sink[0].request.params == {"sku": "WIDGET"}
    assert sink[1].request.body == ""


def test_close_called_on_iterables():
    closed = []

    class Body:
        def __init__(self, chunks):
            self._chunks = chunks

        def __iter__(self):
            return iter(self._chunks)

        def close(self):
            closed.append(True)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return Body([b"hel", b"lo"])

    sink = ListSink()
    recorder = RecordingMiddleware(app, sink, clock=fixed_clock)
    result = recorder(
        {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": "", "wsgi.input": io.BytesIO(b"")},
        lambda status, headers, exc_info=None: None,
    )
    assert closed == [True]
    assert list(result) == [b"hello"]
    assert sink[0].response.body == "hello"


def test_jsonl_sink_round_trip(tmp_path):
    path = tmp_path / "traces.jsonl"
    sink = JsonlSink(path)
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    call_wsgi(app, request_from_spec({"path": "/quote", "query": {"sku": "WIDGET", "qty": "3"}}))
    call_wsgi(app, request_from_spec({"method": "POST", "path": "/invoice", "form": {"customer_id": "1001"}}))
    sink.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == json.dumps(json.loads(lines[0]), sort_keys=True)

    traces = read_traces(path)
    assert [t.id for t in traces] == ["t-000001", "t-000002"]
    assert traces[0].request.params == {"sku": "WIDGET", "qty": "3"}
    assert traces[1].request.params == {"customer_id": "1001"}
    assert traces[0].response.status == 200
    assert traces[0].ts == FIXED.isoformat()


def test_jsonl_sink_truncates_unless_append(tmp_path):
    path = tmp_path / "traces.jsonl"

    with JsonlSink(path) as sink:
        call_wsgi(RecordingMiddleware(echo_app, sink, clock=fixed_clock), request_from_spec({"path": "/a"}))
    with JsonlSink(path) as sink:
        call_wsgi(RecordingMiddleware(echo_app, sink, clock=fixed_clock), request_from_spec({"path": "/b"}))
    assert [t.request.path for t in read_traces(path)] == ["/b"]

    with JsonlSink(path, append=True) as sink:
        call_wsgi(RecordingMiddleware(echo_app, sink, clock=fixed_clock), request_from_spec({"path": "/c"}))
    assert [t.request.path for t in read_traces(path)] == ["/b", "/c"]


def test_list_sink_collects_traces():
    sink = ListSink()
    assert isinstance(sink, list)
    app = RecordingMiddleware(echo_app, sink, clock=fixed_clock)
    call_wsgi(app, request_from_spec({"path": "/quote"}))
    assert len(sink) == 1
    assert sink[0].request.path == "/quote"


def test_expand_sweep_cartesian_order_first_key_slowest():
    sweep = {
        "path": "/pair",
        "query": {"base": "b"},
        "vary": {"a": {"values": ["x", "y"]}, "b": {"values": ["1", "2", "3"]}},
    }
    requests = expand_sweep(sweep)
    assert [r.query_string for r in requests] == [
        "base=b&a=x&b=1",
        "base=b&a=x&b=2",
        "base=b&a=x&b=3",
        "base=b&a=y&b=1",
        "base=b&a=y&b=2",
        "base=b&a=y&b=3",
    ]


def test_expand_sweep_null_omits_parameter():
    sweep = {
        "method": "POST",
        "path": "/invoice",
        "form": {"sku": "WIDGET"},
        "vary": {"customer_id": {"values": ["1001", None]}, "coupon": {"values": [None, "VIP25"]}},
    }
    requests = expand_sweep(sweep)
    assert [r.params for r in requests] == [
        {"sku": "WIDGET", "customer_id": "1001"},
        {"sku": "WIDGET", "customer_id": "1001", "coupon": "VIP25"},
        {"sku": "WIDGET"},
        {"sku": "WIDGET", "coupon": "VIP25"},
    ]


def test_expand_sweep_range_is_inclusive_with_step():
    qty = expand_sweep({"path": "/quote", "vary": {"qty": {"range": [0, 10, 5]}}})
    assert [r.params["qty"] for r in qty] == ["0", "5", "10"]

    full = expand_sweep({"path": "/quote", "query": {"sku": "WIDGET"}, "vary": {"qty": {"range": [0, 1200, 1]}}})
    assert len(full) == 1201
    assert full[0].params == {"sku": "WIDGET", "qty": "0"}
    assert full[-1].params == {"sku": "WIDGET", "qty": "1200"}


def test_expand_sweep_form_versus_query_placement():
    as_query = expand_sweep({"path": "/quote", "query": {"sku": "W"}, "vary": {"qty": {"values": [1]}}})[0]
    assert as_query.method == "GET"
    assert as_query.query_string == "sku=W&qty=1"
    assert as_query.body == ""
    assert as_query.headers == {}

    as_form = expand_sweep({"method": "POST", "path": "/invoice", "form": {"sku": "W"}, "vary": {"qty": {"values": [1]}}})[0]
    assert as_form.method == "POST"
    assert as_form.query_string == ""
    assert as_form.body == "sku=W&qty=1"
    assert as_form.params == {"sku": "W", "qty": "1"}
    assert as_form.headers["content-type"] == "application/x-www-form-urlencoded"


def test_expand_sweep_varied_value_overrides_base_param():
    requests = expand_sweep({"path": "/quote", "query": {"qty": "1", "sku": "W"}, "vary": {"qty": {"values": ["7", "8"]}}})
    assert [r.params for r in requests] == [{"sku": "W", "qty": "7"}, {"sku": "W", "qty": "8"}]
    assert [r.query_string for r in requests] == ["sku=W&qty=7", "sku=W&qty=8"]


def test_raw_query_passthrough():
    request = request_from_spec({"method": "GET", "path": "/x", "raw_query": "a=%20b"})
    assert request.query_string == "a=%20b"
    assert request.params == {"a": " b"}


def test_raw_form_body_passes_repeated_keys_verbatim():
    post = request_from_spec({"method": "POST", "path": "/members", "raw_form": "name=Ivy&name=Oz&tier=premium"})
    assert post.body == "name=Ivy&name=Oz&tier=premium"
    assert post.headers == {"content-type": "application/x-www-form-urlencoded"}


def test_request_from_spec_defaults_and_form_body():
    request = request_from_spec({"path": "/quote", "query": {"sku": "WIDGET", "qty": "3"}})
    assert request.method == "GET"
    assert request.query_string == "sku=WIDGET&qty=3"
    assert request.params == {"sku": "WIDGET", "qty": "3"}

    post = request_from_spec({"method": "POST", "path": "/invoice", "form": {"customer_id": "1001", "sku": "WIDGET"}})
    assert post.body == "customer_id=1001&sku=WIDGET"
    assert post.headers == {"content-type": "application/x-www-form-urlencoded"}
    assert isinstance(post, Request)


def test_expand_workload_explicit_then_sweeps():
    workload = {
        "requests": [{"path": "/a"}, {"path": "/b"}],
        "sweeps": [
            {"path": "/s1", "vary": {"n": {"values": [1, 2]}}},
            {"path": "/s2", "vary": {"n": {"range": [0, 1, 1]}}},
        ],
    }
    paths = [r.path for r in expand_workload(workload)]
    assert paths == ["/a", "/b", "/s1", "/s1", "/s2", "/s2"]
    assert expand_workload({}) == []


def test_load_workload(tmp_path):
    path = tmp_path / "workload.json"
    path.write_text(json.dumps({"requests": [{"path": "/a"}]}), encoding="utf-8")
    assert load_workload(path) == {"requests": [{"path": "/a"}]}


def test_exercise_counts_and_records_into_sink():
    sink = ListSink()
    workload = {
        "requests": [{"path": "/quote", "query": {"sku": "WIDGET"}}],
        "sweeps": [{"path": "/quote", "query": {"sku": "WIDGET"}, "vary": {"qty": {"range": [1, 3, 1]}}}],
    }
    count = exercise(echo_app, workload, sink, clock=fixed_clock)
    assert count == 4
    assert len(sink) == 4
    assert [t.request.params.get("qty") for t in sink] == [None, "1", "2", "3"]
    assert all(t.response.status == 200 for t in sink)
    assert [t.id for t in sink] == ["t-000001", "t-000002", "t-000003", "t-000004"]


def test_record_writes_jsonl_file(tmp_path):
    out = tmp_path / "traces.jsonl"
    workload = {
        "requests": [{"method": "POST", "path": "/invoice", "form": {"customer_id": "1001"}}],
        "sweeps": [{"path": "/missing", "vary": {"n": {"values": [1, 2]}}}],
    }
    count = record(echo_app, workload, out, clock=fixed_clock)
    assert count == 3
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    traces = read_traces(out)
    assert traces[0].request.method == "POST"
    assert traces[0].request.params == {"customer_id": "1001"}
    assert traces[0].response.body
    assert [t.response.status for t in traces] == [200, 404, 404]
    assert traces[2].ts == FIXED.isoformat()
