"""End-to-end: excavate ACME Billing and score the engine against the answer key.

The planted behaviours are listed in legacy_app/ANSWER_KEY.md. Each assertion
below is one behaviour the engine has to rediscover from traffic alone.
"""

import json

import pytest

from archaeology.brief import generate_brief, select_examples
from archaeology.cli import main
from archaeology.infer.claims import load_claims
from archaeology.infer.spec import infer_spec
from archaeology.recorder import ListSink, exercise, load_workload
from legacy_app.billing import make_app


@pytest.fixture(scope="module")
def traces():
    sink = ListSink()
    exercise(make_app(), load_workload("legacy_app/workload.json"), sink)
    return list(sink)


@pytest.fixture(scope="module")
def spec(traces):
    return infer_spec(traces, load_claims("legacy_app/claims.json"))


def _finding(spec, kind, field, sku):
    matches = [
        b for b in spec["boundaries"]
        if b["kind"] == kind and b["field"] == field and b["context"] == {"sku": sku}
    ]
    assert len(matches) == 1, matches
    return matches[0]


def test_documentation_claims_are_judged_correctly(spec):
    verdicts = {c["id"]: c["verdict"] for c in spec["claims"]}
    assert verdicts == {
        "C1": "CONTRADICTED",  # unknown SKU is 200 + error body, not 404
        "C2": "CONTRADICTED",  # SKUs are case-insensitive
        "C3": "CONTRADICTED",  # bulk discount starts at 101, not 100
        "C4": "CONTRADICTED",  # totals truncated, not rounded
        "C5": "CONTRADICTED",  # bad coupons silently ignored
        "C6": "CONTRADICTED",  # unknown customer silently BRONZE
        "C7": "CONFIRMED",
        "C8": "CONTRADICTED",  # qty > 1000 clamped, not rejected
        "C9": "CONFIRMED",
    }


def test_discount_boundaries_are_found_exactly(spec):
    step = _finding(spec, "step", "bulk_discount_pct", "WIDGET")
    assert step["changes_at"] == [101, 500]
    assert [s["value"] for s in step["segments"]] == [0, 10, 15]


def test_quantity_clamp_is_found(spec):
    clamp = _finding(spec, "clamp", "qty", "WIDGET")
    assert clamp["echo_until"] == 1000
    assert clamp["then"] == 1000


def test_non_positive_quantity_changes_response_shape(spec):
    shape = _finding(spec, "shape", "shape", "WIDGET")
    assert shape["changes_at"] == [1]


def test_input_normalisation_is_found(spec):
    found = {(n["endpoint"], n["param"], n["behavior"]) for n in spec["normalization"]}
    for param in ("sku", "coupon"):
        assert ("GET /quote", param, "case-insensitive") in found
        assert ("GET /quote", param, "whitespace-trimmed") in found
    assert ("POST /invoice", "customer_id", "whitespace-trimmed") in found


def test_volatile_fields_are_found(spec):
    paths = {f["path"] for f in spec["volatile_fields"]["POST /invoice"]}
    assert paths == {"invoice_id", "created_at"}
    assert set(spec["volatile_fields"]) == {"POST /invoice"}


def test_brief_includes_evidence_on_both_sides_of_each_boundary(spec, traces):
    examples = select_examples(traces, spec["boundaries"])
    quoted = {
        t.request.params.get("qty")
        for t in examples["GET /quote"]
        if t.request.params.get("sku") == "WIDGET" and set(t.request.params) == {"sku", "qty"}
    }
    assert {"100", "101", "499", "500", "1000", "1001"} <= quoted

    brief = generate_brief(spec, traces, system_name="ACME Billing")
    assert "## Where the old documentation is wrong" in brief
    assert "bulk_discount_pct is 0 for qty 1-100, 10 for qty 101-499" in brief
    # The brief is built from evidence: it must not leak the legacy source.
    assert "PRICES" not in brief and "ROUND_DOWN" not in brief


def test_cli_record_infer_brief_replay(tmp_path, capsys):
    traces = tmp_path / "traces.jsonl"
    spec_path = tmp_path / "spec.json"
    assert main(["record", "--app", "legacy_app.billing:make_app",
                 "--workload", "legacy_app/workload.json", "--out", str(traces)]) == 0
    assert main(["infer", "--traces", str(traces), "--claims", "legacy_app/claims.json",
                 "--spec", str(spec_path), "--report", str(tmp_path / "report.md")]) == 0
    assert main(["brief", "--spec", str(spec_path), "--traces", str(traces),
                 "--out", str(tmp_path / "brief.md")]) == 0
    assert json.loads(spec_path.read_text())["trace_count"] == 1495

    # The legacy system replayed against itself (fresh state) is equivalent,
    # and only because volatile fields are ignored.
    equivalence = tmp_path / "eq.json"
    assert main(["replay", "--traces", str(traces), "--target", "legacy_app.billing:make_app",
                 "--spec", str(spec_path), "--json", str(equivalence)]) == 0
    assert json.loads(equivalence.read_text())["equivalence"] == 1.0
    assert main(["replay", "--traces", str(traces), "--target", "legacy_app.billing:make_app"]) == 1
    assert "EQUIVALENT" in capsys.readouterr().out
