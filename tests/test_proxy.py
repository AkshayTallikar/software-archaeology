"""Tests for the recording reverse proxy and the ``record --target`` / ``proxy`` commands."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from archaeology import cli
from archaeology.cli import main, start_recording_proxy
from archaeology.recorder import ListSink, RecordingProxy, exercise_target
from archaeology.replay import HttpTarget, ProcessTarget
from archaeology.replay.adapters import free_port
from archaeology.trace import read_traces

BILLING = [sys.executable, "-m", "archaeology.serve", "legacy_app.billing:make_app"]

# Two plain requests, a form POST that creates invoices, a small sweep and a
# two-step scenario run for two quantities: 2 + 2 + 2 * 2 = 8 exchanges.
WORKLOAD = {
    "requests": [
        {"path": "/quote", "query": {"sku": "WIDGET", "qty": "3"}},
        {"method": "POST", "path": "/invoice", "form": {"customer_id": "1001", "sku": "WIDGET", "qty": "2"}},
    ],
    "sweeps": [{"path": "/quote", "query": {"sku": "WIDGET"}, "vary": {"qty": {"values": [1, 2]}}}],
    "scenarios": [{
        "name": "invoice-then-quote",
        "vary": {"qty": {"values": [1, 2]}},
        "steps": [
            {"method": "POST", "path": "/invoice",
             "form": {"customer_id": "1002", "sku": "GADGET", "qty": "{{qty}}"},
             "capture": {"inv": "invoice_id"}},
            {"method": "GET", "path": "/quote", "query": {"sku": "GADGET", "qty": "{{qty}}", "ref": "{{inv}}"}},
        ],
    }],
}
WORKLOAD_EXCHANGES = 8


@pytest.fixture(scope="module")
def billing():
    """The bundled billing system running as a real HTTP server."""
    with ProcessTarget(BILLING) as target:
        yield target


def test_proxy_records_the_same_exchanges_the_client_saw(billing):
    proxy_sink = ListSink()
    client_sink = ListSink()
    with RecordingProxy(billing.base_url, proxy_sink) as proxy:
        with HttpTarget(proxy.url) as client:
            assert exercise_target(client, WORKLOAD, client_sink) == WORKLOAD_EXCHANGES

    assert len(proxy_sink) == len(client_sink) == WORKLOAD_EXCHANGES
    for recorded, seen in zip(proxy_sink, client_sink):
        assert recorded.request.method == seen.request.method
        assert recorded.request.path == seen.request.path
        assert recorded.request.params == seen.request.params
        assert recorded.request.query_string == seen.request.query_string
        assert recorded.request.body == seen.request.body
        assert recorded.response.status == seen.response.status
        # the proxy never alters a body: invoice ids and timestamps match too
        assert recorded.response.body == seen.response.body
        assert recorded.response.json() == seen.response.json()
    assert [t.id for t in proxy_sink] == [f"t-{i:06d}" for i in range(1, WORKLOAD_EXCHANGES + 1)]
    # the proxy sees only HTTP, so it records no scenario tags; the client's exerciser tags them
    assert all(t.scenario is None for t in proxy_sink)
    assert client_sink[4].scenario == {"name": "invoice-then-quote", "run": 0, "step": 0, "vars": {"qty": "1"}}
    assert client_sink[-1].scenario == {"name": "invoice-then-quote", "run": 1, "step": 1, "vars": {"qty": "2"}}


def test_proxy_forwards_form_posts_and_sets_content_type(billing):
    sink = ListSink()
    with RecordingProxy(billing.base_url, sink) as proxy:
        response = httpx.post(f"{proxy.url}/invoice", data={"customer_id": "1001", "sku": "WIDGET", "qty": "1"})
    assert response.status_code == 200
    assert response.json()["invoice_id"].startswith("INV-")
    trace = sink[0]
    assert trace.request.headers == {"content-type": "application/x-www-form-urlencoded"}
    assert trace.request.params == {"customer_id": "1001", "sku": "WIDGET", "qty": "1"}


def test_upstream_errors_are_answered_with_502_and_not_recorded():
    sink = ListSink()
    dead_port = free_port()
    with RecordingProxy(f"http://127.0.0.1:{dead_port}", sink) as proxy:
        response = httpx.get(f"{proxy.url}/quote", params={"sku": "WIDGET"})
        assert response.status_code == 502
        assert response.json() == {"error": "UPSTREAM_UNAVAILABLE"}
        assert proxy.count == 0
    assert sink == []


def test_concurrent_requests_get_unique_ids(billing):
    sink = ListSink()
    with RecordingProxy(billing.base_url, sink) as proxy:
        with httpx.Client(base_url=proxy.url, timeout=10.0) as client:
            def fetch(qty: int) -> int:
                return client.get("/quote", params={"sku": "WIDGET", "qty": str(qty)}).status_code

            with ThreadPoolExecutor(max_workers=4) as pool:
                statuses = list(pool.map(fetch, range(1, 21)))
        assert statuses == [200] * 20
        assert proxy.count == 20
    assert len(sink) == 20
    assert len({t.id for t in sink}) == 20
    assert {t.id for t in sink} == {f"t-{i:06d}" for i in range(1, 21)}


def test_cli_record_target_writes_the_workload_traces(tmp_path):
    workload = tmp_path / "workload.json"
    workload.write_text(json.dumps(WORKLOAD), encoding="utf-8")
    out = tmp_path / "traces.jsonl"
    spec = f"cmd:{sys.executable} -m archaeology.serve legacy_app.billing:make_app"
    assert main(["record", "--target", spec, "--workload", str(workload), "--out", str(out)]) == 0
    assert len(read_traces(out)) == WORKLOAD_EXCHANGES


def test_cli_record_app_still_works(tmp_path):
    workload = tmp_path / "workload.json"
    workload.write_text(json.dumps(WORKLOAD), encoding="utf-8")
    out = tmp_path / "traces.jsonl"
    assert main(["record", "--app", "legacy_app.billing:make_app", "--workload", str(workload), "--out", str(out)]) == 0
    assert len(read_traces(out)) == WORKLOAD_EXCHANGES


@pytest.mark.parametrize("extra", [
    [],
    ["--app", "legacy_app.billing:make_app", "--target", "http://127.0.0.1:1"],
])
def test_cli_record_needs_exactly_one_source(tmp_path, extra):
    workload = tmp_path / "workload.json"
    workload.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["record", *extra, "--workload", str(workload), "--out", str(tmp_path / "t.jsonl")])


def test_proxy_helper_records_to_a_file_on_port_zero(billing, tmp_path):
    out = tmp_path / "proxy.jsonl"
    proxy, sink = start_recording_proxy(billing.base_url, "127.0.0.1:0", out)
    try:
        assert proxy.port > 0
        assert httpx.get(f"{proxy.url}/quote", params={"sku": "WIDGET", "qty": "2"}).status_code == 200
    finally:
        proxy.stop()
        sink.close()
    traces = read_traces(out)
    assert [t.request.path for t in traces] == ["/quote"]
    assert traces[0].response.status == 200


def test_proxy_command_records_until_interrupted(billing, tmp_path, monkeypatch):
    port = free_port()
    out = tmp_path / "proxy.jsonl"

    def send_one_then_interrupt() -> None:
        assert httpx.get(f"http://127.0.0.1:{port}/quote", params={"sku": "GIZMO"}).status_code == 200
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_wait_forever", send_one_then_interrupt)
    code = main(["proxy", "--upstream", billing.base_url, "--listen", f"127.0.0.1:{port}", "--out", str(out)])
    assert code == 0
    assert len(read_traces(out)) == 1
