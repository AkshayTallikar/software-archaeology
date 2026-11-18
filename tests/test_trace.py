from archaeology.trace import Request, Response, Trace, read_traces, write_traces
from archaeology.wsgi import call_wsgi


def test_build_merges_query_and_form_params():
    req = Request.build(
        "post",
        "/invoice",
        query_string="sku=WIDGET&qty=1",
        body="qty=3&customer_id=1001",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    assert req.method == "POST"
    assert req.content_type == "application/x-www-form-urlencoded"
    assert req.params == {"sku": "WIDGET", "qty": "3", "customer_id": "1001"}


def test_non_form_body_is_not_parsed_into_params():
    req = Request.build("POST", "/x", body='{"a": 1}', headers={"Content-Type": "application/json"})
    assert req.params == {}


def test_key_ignores_param_order():
    a = Request.build("GET", "/q", "a=1&b=2")
    b = Request.build("GET", "/q", "b=2&a=1")
    assert a.key() == b.key()


def test_response_json():
    assert Response(200, {}, '{"a": 1}').json() == {"a": 1}
    assert Response(400, {}, "bad qty").json() is None


def test_round_trip(tmp_path):
    trace = Trace("t-000001", "2026-01-01T00:00:00+00:00", Request.build("GET", "/q", "a=1"), Response(200, {"content-type": "text/plain"}, "ok"))
    path = tmp_path / "traces.jsonl"
    assert write_traces(path, [trace, trace]) == 2
    assert read_traces(path) == [trace, trace]


def test_call_wsgi_passes_body_and_captures_response():
    def app(environ, start_response):
        start_response("201 Created", [("Content-Type", "text/plain")])
        return [environ["wsgi.input"].read()]

    resp = call_wsgi(app, Request.build("POST", "/x", body="hello"))
    assert resp.status == 201
    assert resp.headers == {"content-type": "text/plain"}
    assert resp.body == "hello"
