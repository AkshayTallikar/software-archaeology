"""Replay recorded traces against a target and report equivalence."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from archaeology.trace import FORM_CONTENT_TYPE, Request, Trace

from .adapters import Target
from .compare import compare


@dataclass
class ReplayResult:
    """The outcome of replaying one trace."""

    trace_id: str
    request_line: str
    matched: bool
    differences: list[str] = field(default_factory=list)


@dataclass
class ReplayReport:
    """Results of a replay run, with summary properties."""

    results: list[ReplayResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def matched(self) -> int:
        return sum(1 for r in self.results if r.matched)

    @property
    def mismatched(self) -> list[ReplayResult]:
        return [r for r in self.results if not r.matched]

    @property
    def equivalence(self) -> float:
        """Fraction of responses that matched; 1.0 for an empty report."""
        if self.total == 0:
            return 1.0
        return self.matched / self.total

    def passed(self, threshold: float = 1.0) -> bool:
        """True when ``equivalence`` is at least ``threshold``."""
        return self.equivalence >= threshold


def _request_line(request: Request) -> str:
    """Human-readable request, e.g. ``GET /quote?sku=WIDGET`` or a form POST."""
    line = f"{request.method} {request.path}"
    if request.query_string:
        line += f"?{request.query_string}"
    if request.body and request.content_type == FORM_CONTENT_TYPE:
        line += f"  {request.body}"
    return line


def _ignore_for(
    ignore: set[str] | Callable[[Request], set[str]] | None,
    request: Request,
) -> set[str]:
    if ignore is None:
        return set()
    if callable(ignore):
        return set(ignore(request))
    return set(ignore)


def replay(
    traces: Iterable[Trace],
    target: Target,
    ignore: set[str] | Callable[[Request], set[str]] | None = None,
    on_result: Callable[[ReplayResult], None] | None = None,
) -> ReplayReport:
    """Replay ``traces`` in order against ``target``.

    ``ignore`` is a set of volatile JSON paths, or a function that returns
    one per request. An exception raised by the target is recorded as a
    mismatch rather than stopping the run. ``on_result`` is called after
    each trace.
    """
    report = ReplayReport()
    for trace in traces:
        ignore_paths = _ignore_for(ignore, trace.request)
        try:
            actual = target(trace.request)
        except Exception as exc:  # any target failure counts as a mismatch
            differences = [f"error: {type(exc).__name__}: {exc}"]
        else:
            differences = compare(trace.response, actual, ignore_paths)
        result = ReplayResult(
            trace_id=trace.id,
            request_line=_request_line(trace.request),
            matched=not differences,
            differences=differences,
        )
        report.results.append(result)
        if on_result is not None:
            on_result(result)
    return report


def render_markdown(report: ReplayReport, max_failures: int = 20) -> str:
    """Render the report as Markdown, listing up to ``max_failures`` mismatches."""
    verdict = "EQUIVALENT" if report.passed() else "NOT EQUIVALENT"
    lines = [
        "# Equivalence report",
        "",
        f"**{report.matched} / {report.total} responses equivalent "
        f"({report.equivalence * 100:.1f}%)**",
        "",
        f"Verdict: {verdict}",
        "",
    ]
    failures = report.mismatched
    if failures:
        lines += ["## Mismatches", ""]
        shown = failures[: max(max_failures, 0)]
        for result in shown:
            lines.append(f"### {result.trace_id} `{result.request_line}`")
            lines.append("")
            lines += [f"- {difference}" for difference in result.differences]
            lines.append("")
        hidden = len(failures) - len(shown)
        if hidden > 0:
            lines.append(f"... and {hidden} more")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_dict(report: ReplayReport) -> dict[str, Any]:
    """A JSON-serialisable summary of the report, with its mismatches."""
    return {
        "total": report.total,
        "matched": report.matched,
        "equivalence": report.equivalence,
        "mismatches": [
            {
                "trace_id": r.trace_id,
                "request": r.request_line,
                "differences": list(r.differences),
            }
            for r in report.mismatched
        ],
    }
