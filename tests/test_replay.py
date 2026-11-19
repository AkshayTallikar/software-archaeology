"""Tests for the replay engine: adapters, comparator, runner and report."""

import itertools
import json
import sys
import threading
from urllib.parse import parse_qsl
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from archaeology.replay import (
    AsgiTarget,
    HttpTarget,
    ReplayReport,
    ReplayResult,
    WsgiTarget,
    compare,
    load_target,
    render_markdown,
    replay,
    to_dict,
)
from archaeology.replay.runner import replay as runner_replay
from archaeology.trace import FORM_CONTENT_TYPE, Request, Response, Trace
from archaeology.wsgi import call_wsgi

# --- tiny apps with identical behaviour -----------------------------------


def _json_response(start_response, data):
    body = json.dumps(data).encode("utf-8")
    start_response(
        "200 OK",
        [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
    )
    return [body]


def make_wsgi_app(start=1):
    """WSGI reference app. ``id`` counts /hello calls from ``start``."""
    counter = itertools.count(start)

    def app(environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ["PATH_INFO"]
        if method == "GET" and path == "/hello":
            query = dict(parse_qsl(environ["QUERY_STRING"], keep_blank_values=True))
            name = query.get("name", "")
            return _json_response(
                start_response, {"greeting": f"hi {name}", "id": next(counter)}
            )
        if method == "POST" and path == "/echo":
            length = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(length).decode("utf-8")
            return _json_response(start_response, dict(parse_qsl(raw, keep_blank_values=True)))
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]

    return app


def make_fastapi_app(start=1, broken=False):
    """FastAPI twin of :func:`make_wsgi_app`. ``broken`` capitalises the greeting."""
    app = FastAPI()
    counter = itertools.count(start)

    @app.get("/hello")
    def hello(name: str = "") -> dict:
        greeting = f"Hi {name}" if broken else f"hi {name}"
        return {"greeting": greeting, "id": next(counter)}

    @app.post("/echo")
    async def echo(request: FastAPIRequest) -> dict:
        raw = (await request.body()).decode("utf-8")
        return dict(parse_qsl(raw, keep_blank_values=True))

    @app.exception_handler(StarletteHTTPException)
    async def not_found(request, exc):
        return PlainTextResponse("not found", status_code=404)

    return app


def sample_requests():
    form = {"Content-Type": FORM_CONTENT_TYPE}
    return [
        Request.build("GET", "/hello", "name=Ada"),
        Request.build("GET", "/hello", "name=Grace"),
        Request.build("GET", "/hello", "name=%20Bob%20"),
        Request.build("GET", "/missing"),
        Request.build("POST", "/hello", "name=x"),
        Request.build("POST", "/echo", body="customer_id=1001&sku=WIDGET", headers=form),
    ]


def record(app, requests):
    """Record expected traces by calling the reference WSGI app."""
    return [
        Trace(
            id=f"t-{i:06d}",
            ts="2026-11-18T00:00:00+00:00",
            request=req,
            response=call_wsgi(app, req),
        )
        for i, req in enumerate(requests, start=1)
    ]


def resp(data, status=200, content_type="application/json"):
    return Response(
        status=status,
        headers={"content-type": content_type},
        body=json.dumps(data),
    )


def text(body, status=200, content_type="text/plain"):
    return Response(status=status, headers={"content-type": content_type}, body=body)


# --- compare() -------------------------------------------------------------


def test_identical_responses_have_no_differences():
    assert compare(resp({"a": [1, {"b": "x"}]}), resp({"a": [1, {"b": "x"}]})) == []


def test_status_difference():
    diffs = compare(resp({}), resp({}, status=404))
    assert diffs == ["status: expected 200, got 404"]


def test_content_type_difference_is_exact():
    diffs = compare(resp({}), text("{}", content_type="text/plain"))
    assert "content-type: expected 'application/json', got 'text/plain'" in diffs


def test_content_type_ignores_parameters_and_case():
    expected = text("hi", content_type="text/plain; charset=utf-8")
    actual = text("hi", content_type="Text/Plain")
    assert compare(expected, actual) == []


def test_json_nested_and_list_paths():
    expected = resp({"order": {"lines": [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 2}]}})
    actual = resp({"order": {"lines": [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 3}]}})
    assert compare(expected, actual) == ["body.order.lines[1].qty: expected 2, got 3"]


def test_missing_and_unexpected_keys():
    diffs = compare(resp({"a": 1, "b": 2}), resp({"a": 1, "c": 3}))
    assert diffs == ["body.b: missing in actual", "body.c: unexpected in actual"]


def test_list_length_mismatch_still_compares_common_prefix():
    assert compare(resp([1, 2, 3]), resp([1, 2])) == ["body: expected 3 items, got 2"]
    assert compare(resp([1, 2, 3]), resp([9, 2])) == [
        "body: expected 3 items, got 2",
        "body[0]: expected 1, got 9",
    ]


def test_ignore_paths_skip_values_but_not_keys():
    expected = resp(
        {
            "id": 1,
            "invoice_id": "x",
            "line": [{"sku": "A", "n": 1}, {"sku": "B", "n": 2}],
        }
    )
    actual = resp(
        {
            "id": 99,
            "invoice_id": "y",
            "line": [{"sku": "Z", "n": 1}, {"sku": "Q", "n": 2}],
        }
    )
    assert compare(expected, actual, ignore={"id", "invoice_id", "line.sku"}) == []

    changed = resp({"id": 99, "invoice_id": "y", "line": [{"sku": "Z", "n": 9}, {"sku": "Q", "n": 2}]})
    diffs = compare(expected, changed, ignore={"id", "invoice_id", "line.sku"})
    assert diffs == ["body.line[0].n: expected 1, got 9"]


def test_ignored_key_must_still_exist_on_both_sides():
    assert compare(resp({"id": 1}), resp({}), ignore={"id"}) == ["body.id: missing in actual"]


def test_ignore_is_exact_dotted_path():
    diffs = compare(resp({"x": {"id": 1}}), resp({"x": {"id": 2}}), ignore={"id"})
    assert diffs == ["body.x.id: expected 1, got 2"]


def test_int_versus_string_differs():
    assert compare(resp({"n": 1}), resp({"n": "1"})) == ['body.n: expected 1, got "1"']


def test_bool_versus_int_differs():
    assert compare(resp({"ok": True}), resp({"ok": 1})) == ["body.ok: expected true, got 1"]


def test_int_and_float_are_equal():
    assert compare(resp({"p": 1}), resp({"p": 1.0})) == []


def test_non_json_actual_when_json_expected():
    actual = Response(status=200, headers={"content-type": "application/json"}, body="<html>oops</html>")
    assert compare(resp({"a": 1}), actual) == [
        "body: expected JSON, got non-JSON '<html>oops</html>'"
    ]


def test_null_body_counts_as_json():
    expected = Response(status=200, headers={"content-type": "application/json"}, body="null")
    actual = Response(status=200, headers={"content-type": "application/json"}, body="")
    assert compare(expected, actual) == ["body: expected JSON, got non-JSON ''"]


def test_text_body_is_rstripped():
    assert compare(text("hello\n\n"), text("hello  \n")) == []


def test_text_body_difference_is_truncated_to_120_chars():
    diffs = compare(text("a" * 200), text("b" * 200))
    assert diffs == [f"body: expected '{'a' * 120}', got '{'b' * 120}'"]


# --- adapters --------------------------------------------------------------


def test_wsgi_and_fastapi_equivalent_when_ignoring_id():
    traces = record(make_wsgi_app(1), sample_requests())
    with AsgiTarget(make_fastapi_app(start=100)) as target:
        report = replay(traces, target, ignore={"id"})
    assert report.total == len(traces)
    assert report.equivalence == 1.0
    assert report.passed()


def test_volatile_id_is_a_mismatch_without_ignore():
    traces = record(make_wsgi_app(1), sample_requests())
    with AsgiTarget(make_fastapi_app(start=100)) as target:
        report = replay(traces, target)
    # The three GET /hello calls carry a counter value that differs.
    assert report.matched == len(traces) - 3
    assert {r.request_line for r in report.mismatched} == {
        "GET /hello?name=Ada",
        "GET /hello?name=Grace",
        "GET /hello?name=%20Bob%20",
    }
    assert report.mismatched[0].differences == ["body.id: expected 1, got 100"]


def test_broken_variant_reports_readable_diffs():
    traces = record(make_wsgi_app(1), sample_requests())
    with AsgiTarget(make_fastapi_app(start=1, broken=True)) as target:
        report = replay(traces, target, ignore={"id"})
    assert not report.passed()
    assert report.matched == len(traces) - 3
    first = report.mismatched[0]
    assert first.trace_id == "t-000001"
    assert first.request_line == "GET /hello?name=Ada"
    assert first.differences == ['body.greeting: expected "hi Ada", got "Hi Ada"']
    md = render_markdown(report)
    assert "Verdict: NOT EQUIVALENT" in md
    assert '- body.greeting: expected "hi Ada", got "Hi Ada"' in md


def test_raw_query_string_reaches_app_unchanged():
    req = Request.build("GET", "/hello", "name=%20Bob%20")
    assert json.loads(WsgiTarget(make_wsgi_app())(req).body)["greeting"] == "hi  Bob "
    with AsgiTarget(make_fastapi_app()) as target:
        assert json.loads(target(req).body)["greeting"] == "hi  Bob "


def test_asgi_target_close_is_idempotent():
    target = AsgiTarget(make_fastapi_app())
    target.close()
    target.close()
    with pytest.raises(RuntimeError):
        target(Request.build("GET", "/hello"))


@pytest.fixture
def live_server():
    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    server = make_server("127.0.0.1", 0, make_wsgi_app(1), handler_class=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_target_passes_raw_query_and_form_body(live_server):
    with HttpTarget(live_server) as target:
        got = target(Request.build("GET", "/hello", "name=%20Bob%20"))
        assert got.status == 200
        assert got.content_type == "application/json"
        assert json.loads(got.body)["greeting"] == "hi  Bob "

        form = target(
            Request.build(
                "POST",
                "/echo",
                body="a=1&b=2",
                headers={"content-type": FORM_CONTENT_TYPE},
            )
        )
        assert json.loads(form.body) == {"a": "1", "b": "2"}


# --- load_target -----------------------------------------------------------

FIXTURE_MODULE = '''
from fastapi import FastAPI

def wsgi_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]

app = FastAPI()

NOT_CALLABLE = 3
'''


def test_load_target_picks_adapter(tmp_path, monkeypatch):
    (tmp_path / "replay_fixture_apps.py").write_text(FIXTURE_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "replay_fixture_apps", raising=False)

    wsgi = load_target("replay_fixture_apps:wsgi_app")
    assert isinstance(wsgi, WsgiTarget)
    assert wsgi(Request.build("GET", "/")).body == "ok"

    asgi = load_target("replay_fixture_apps:app")
    assert isinstance(asgi, AsgiTarget)
    asgi.close()

    with pytest.raises(TypeError):
        load_target("replay_fixture_apps:NOT_CALLABLE")
    with pytest.raises(ValueError):
        load_target("no_colon_here")


def test_load_target_http_url_returns_http_target():
    target = load_target("http://127.0.0.1:9")
    assert isinstance(target, HttpTarget)
    target.close()


# --- runner and report -----------------------------------------------------


def test_target_exception_is_captured_as_mismatch():
    def boom(request):
        raise ValueError("boom")

    traces = record(make_wsgi_app(1), sample_requests()[:2])
    report = replay(traces, boom)
    assert report.matched == 0
    assert report.results[0].differences == ["error: ValueError: boom"]
    assert report.results[0].matched is False


def test_callable_ignore_is_called_per_request():
    traces = record(make_wsgi_app(1), sample_requests())
    seen = []

    def ignore_for(request):
        seen.append(request.path)
        return {"id"} if request.path == "/hello" else set()

    report = replay(traces, WsgiTarget(make_wsgi_app(50)), ignore=ignore_for)
    assert report.passed()
    assert seen == [t.request.path for t in traces]


def test_on_result_called_for_each_trace_in_order():
    traces = record(make_wsgi_app(1), sample_requests())
    seen: list[ReplayResult] = []
    report = runner_replay(traces, WsgiTarget(make_wsgi_app(1)), on_result=seen.append)
    assert [r.trace_id for r in seen] == [t.id for t in traces]
    assert seen == report.results
    assert report.passed()


def test_request_lines_for_get_and_form_post():
    traces = record(make_wsgi_app(1), sample_requests())
    report = replay(traces, WsgiTarget(make_wsgi_app(1)))
    assert report.results[0].request_line == "GET /hello?name=Ada"
    assert report.results[-1].request_line == "POST /echo  customer_id=1001&sku=WIDGET"


def test_empty_report_is_equivalent():
    report = ReplayReport()
    assert report.total == 0
    assert report.equivalence == 1.0
    assert report.passed()


def test_equivalence_and_threshold():
    report = ReplayReport(
        results=[
            ReplayResult("t-1", "GET /a", True),
            ReplayResult("t-2", "GET /b", True),
            ReplayResult("t-3", "GET /c", False, ["status: expected 200, got 500"]),
        ]
    )
    assert report.matched == 2
    assert [r.trace_id for r in report.mismatched] == ["t-3"]
    assert report.equivalence == pytest.approx(2 / 3)
    assert report.passed(0.5)
    assert not report.passed()


def test_render_markdown_when_equivalent():
    report = ReplayReport(
        results=[ReplayResult("t-1", "GET /a", True), ReplayResult("t-2", "GET /b", True)]
    )
    md = render_markdown(report)
    assert md.startswith("# Equivalence report")
    assert "**2 / 2 responses equivalent (100.0%)**" in md
    assert "Verdict: EQUIVALENT" in md
    assert "## Mismatches" not in md


def test_render_markdown_truncates_mismatches():
    results = [
        ReplayResult(f"t-{i:06d}", "GET /x", False, [f"body.a: expected {i}, got 0"])
        for i in range(5)
    ]
    md = render_markdown(ReplayReport(results=results), max_failures=2)
    assert "## Mismatches" in md
    assert md.count("### t-") == 2
    assert "### t-000000 `GET /x`" in md
    assert "- body.a: expected 0, got 0" in md
    assert "... and 3 more" in md
    assert "Verdict: NOT EQUIVALENT" in md


def test_to_dict_summary():
    report = ReplayReport(
        results=[
            ReplayResult("t-1", "GET /a", True),
            ReplayResult("t-2", "GET /b", False, ["status: expected 200, got 500"]),
        ]
    )
    assert to_dict(report) == {
        "total": 2,
        "matched": 1,
        "equivalence": 0.5,
        "remapped": 0,
        "mismatches": [
            {
                "trace_id": "t-2",
                "request": "GET /b",
                "differences": ["status: expected 200, got 500"],
            }
        ],
    }
