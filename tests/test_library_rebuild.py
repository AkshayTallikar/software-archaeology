"""The Pinecrest Library rebuild, written by a coding agent from the brief alone.

The rebuild must replay the legacy system's recorded traffic exactly. To show
that this proof has teeth, each test in the mutation group changes one business
rule of the rebuild by a small amount and checks that replay notices.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from archaeology.infer import infer_spec
from archaeology.infer.spec import volatile_paths_for
from archaeology.recorder import ListSink, exercise_target, load_workload
from archaeology.replay import AsgiTarget, ProcessTarget, replay
from modern_library import main as rebuild

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture(scope="module")
def recording():
    """Record the legacy Node.js service once and infer its spec."""
    sink = ListSink()
    with ProcessTarget(f"node {ROOT / 'legacy_library' / 'server.js'}") as target:
        exercise_target(target, load_workload(ROOT / "legacy_library" / "workload.json"), sink)
    traces = list(sink)
    return traces, infer_spec(traces)


def _replay(recording):
    traces, spec = recording
    return replay(traces, AsgiTarget(rebuild.make_app()), ignore=lambda r: volatile_paths_for(spec, r))


def test_rebuild_replays_the_legacy_recording_exactly(recording) -> None:
    report = _replay(recording)
    assert report.total > 1000
    assert report.passed(1.0), report.mismatched[:3]
    assert report.remapped > 0


@pytest.mark.parametrize(
    ("constant", "value"),
    [
        ("FEE_CAP_CENTS", 975),
        ("FEE_FREE_DAYS", 3),
        ("FEE_STEP_CENTS", 30),
        ("FEE_BORROW_BLOCK_CENTS", 525),
        ("MAX_ACTIVE_LOANS", 4),
        ("PER_PAGE_MAX", 49),
    ],
)
def test_replay_catches_a_small_change_to_any_business_rule(recording, monkeypatch, constant, value) -> None:
    monkeypatch.setattr(rebuild, constant, value)
    assert not _replay(recording).passed(1.0)
