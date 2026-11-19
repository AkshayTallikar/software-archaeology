"""The ``demo`` command's bundled systems."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from archaeology.cli import DEMO_SYSTEMS, build_parser, main

ROOT = Path(__file__).resolve().parent.parent


def test_every_demo_system_points_at_real_files() -> None:
    for name, system in DEMO_SYSTEMS.items():
        assert (ROOT / system.workload).is_file(), name
        assert (ROOT / system.claims).is_file(), name


def test_demo_defaults_to_billing_and_its_bundled_rebuild() -> None:
    args = build_parser().parse_args(["demo"])
    assert args.system == "billing"
    assert args.target is None
    assert DEMO_SYSTEMS["billing"].rebuild == "modern_app.main:make_app"


def test_library_demo_probes_scenarios() -> None:
    library = DEMO_SYSTEMS["library"]
    assert library.stateful
    assert library.reference.startswith("cmd:node ")


def test_billing_demo_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    status = main(["demo", "--out", str(tmp_path), "--probe-count", "200"])
    assert status == 0
    for name in ("traces.jsonl", "spec.json", "brief.md", "equivalence.json", "probe.json", "report.html"):
        assert (tmp_path / name).is_file(), name


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_library_reference_records_through_the_demo_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    library = DEMO_SYSTEMS["library"]
    out = tmp_path / "traces.jsonl"
    assert main(["record", "--target", library.reference, "--workload", library.workload, "--out", str(out)]) == 0
    assert sum(1 for _ in out.open()) > 1000
