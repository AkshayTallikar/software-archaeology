import json

from archaeology.infer.spec import infer_spec
from archaeology.probe import generate_requests, probe, render_markdown
from archaeology.recorder import ListSink, exercise
from archaeology.replay import WsgiTarget
from archaeology.trace import FORM_CONTENT_TYPE, Response
from legacy_app.billing import make_app

WORKLOAD = {
    "requests": [
        {"method": "POST", "path": "/invoice", "form": {"customer_id": "1001", "sku": "WIDGET", "qty": "2"}},
        {"method": "POST", "path": "/invoice", "form": {"customer_id": "1001", "sku": "WIDGET", "qty": "2"}},
    ],
    "sweeps": [
        {"method": "GET", "path": "/quote", "query": {}, "vary": {
            "sku": {"values": ["WIDGET", "GADGET"]},
            "coupon": {"values": [None, "SAVE10"]},
            "qty": {"range": [1, 600, 50]},
        }},
    ],
}


def _recorded():
    sink = ListSink()
    exercise(make_app(), WORKLOAD, sink)
    traces = list(sink)
    return traces, infer_spec(traces)


def test_generation_is_deterministic_and_stays_within_observed_inputs():
    traces, spec = _recorded()
    a = generate_requests(spec, traces, 200, seed=3)
    b = generate_requests(spec, traces, 200, seed=3)
    assert [(r.method, r.path, r.query_string, r.body) for r in a] == [
        (r.method, r.path, r.query_string, r.body) for r in b
    ]
    quotes = [r for r in a if r.path == "/quote"]
    assert quotes and all(r.params.get("sku", "WIDGET") in {"WIDGET", "GADGET"} for r in quotes)
    assert all(1 <= int(r.params["qty"]) <= 551 for r in quotes if "qty" in r.params)
    # Quantities are drawn across the range, not just replayed from the recording.
    recorded_qty = {t.request.params.get("qty") for t in traces}
    assert any(r.params.get("qty") not in recorded_qty for r in quotes)
    invoices = [r for r in a if r.path == "/invoice"]
    assert invoices and all(r.content_type == FORM_CONTENT_TYPE for r in invoices)


def test_identical_systems_agree_with_volatile_fields_ignored():
    traces, spec = _recorded()
    requests = generate_requests(spec, traces, 300, seed=1)
    report = probe(WsgiTarget(make_app()), WsgiTarget(make_app()), requests, spec)
    assert report.total == 300
    assert report.divergences == []
    assert "NO DIVERGENCE FOUND" in render_markdown(report)


def test_probe_catches_a_rounding_difference_replay_never_saw():
    traces, spec = _recorded()
    legacy = WsgiTarget(make_app())

    def rounds_instead_of_truncating(request):
        response = legacy(request)
        body = response.json()
        if isinstance(body, dict) and "total" in body and request.path == "/quote":
            cents = round(float(body["subtotal"]) * 100)
            if cents % 7 == 3:  # perturb a slice of inputs
                body["total"] = "0.00"
                return Response(response.status, response.headers, json.dumps(body))
        return response

    requests = generate_requests(spec, traces, 500, seed=2)
    report = probe(WsgiTarget(make_app()), rounds_instead_of_truncating, requests, spec)
    assert report.divergences
    assert all(d.request.path == "/quote" for d in report.divergences)
    assert "body.total" in report.divergences[0].differences[0]
    assert "DIVERGENT" in render_markdown(report)
