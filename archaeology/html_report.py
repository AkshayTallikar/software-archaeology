"""A self-contained HTML evidence report for one legacy system.

The page answers, in one file that opens offline: what the documentation
claims and what the traffic shows, every hidden behaviour with a chart of its
evidence, how identifiers flow between requests, which fields are volatile,
and the replay and probe verdicts when those are supplied.

The page has no scripts, no external resources and no network references.
Charts are inline SVG recomputed from the recorded traces, so the file stays
small: only the points a chart plots are embedded. All recorded text is
escaped, because it comes from traffic. Output is deterministic.
"""

from __future__ import annotations

import html
import json
from datetime import date
from typing import Any, Iterable, Iterator, Sequence

from .brief import identifier_formats
from .infer.boundaries import evidence_note
from .infer.catalog import endpoint_key, json_object, value_type
from .trace import Trace

MAX_LISTED = 20
MAX_LINE_POINTS = 200  # longer series are thinned; points beside a change are always kept
MAX_DOTS = 60  # dots are drawn only for short series, to keep the page small
PLOT_W, PLOT_H = 640, 200
BAND_H = 64
LEFT, RIGHT, TOP, BOTTOM = 56, 16, 18, 34

_VERDICT_CLASS = {"CONFIRMED": "ok", "CONTRADICTED": "bad", "UNOBSERVED": "grey"}

_CSS = """
:root{color-scheme:light dark;--bg:#f5f6f8;--panel:#ffffff;--text:#1c2330;--muted:#5d6676;--line:#d7dce4;
--accent:#2f5bd3;--ok:#16794a;--bad:#b42318;--warn:#8a5a00;--grey:#5d6676;
--c0:#4c78a8;--c1:#e0791a;--c2:#4f9a47;--c3:#a8649c;--c4:#d64f4f;--c5:#3c9c96}
@media (prefers-color-scheme: dark){:root{--bg:#0f1319;--panel:#171c24;--text:#e6e9ef;--muted:#9aa3b2;
--line:#2c3440;--accent:#7ea6ff;--ok:#5fd29a;--bad:#ff8a80;--warn:#f4c36b;--grey:#9aa3b2;
--c0:#6c9bd2;--c1:#ffa94d;--c2:#7cc474;--c3:#c99ac0;--c4:#ff7b7b;--c5:#8fd3cd}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.6rem;margin:0 0 4px}
h2{font-size:1.2rem;margin:32px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:1rem;margin:0 0 4px;overflow-wrap:anywhere}
p{margin:6px 0}
.muted{color:var(--muted)}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid currentColor;font-size:.85rem;white-space:nowrap}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.grey{color:var(--grey)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;overflow-wrap:anywhere}
svg{width:100%;height:auto;display:block;margin-top:8px}
svg text{fill:var(--muted);font:11px system-ui,sans-serif}
.axis{stroke:var(--line);stroke-width:1}
.line{fill:none;stroke:var(--accent);stroke-width:1.6}
.pt{fill:var(--accent)}
.chg{stroke:var(--warn);stroke-width:1.2;stroke-dasharray:4 3}
.c0{fill:var(--c0);background:var(--c0)}.c1{fill:var(--c1);background:var(--c1)}
.c2{fill:var(--c2);background:var(--c2)}.c3{fill:var(--c3);background:var(--c3)}
.c4{fill:var(--c4);background:var(--c4)}.c5{fill:var(--c5);background:var(--c5)}
.legend{list-style:none;padding:0;margin:8px 0 0;display:flex;flex-wrap:wrap;gap:4px 14px;font-size:.88rem}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.meta{font-size:.88rem;color:var(--muted);margin:2px 0 6px}
.note{font-size:.9rem;color:var(--muted);margin:8px 0 0}
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _position(raw: Any) -> tuple[str, int] | None:
    """``(axis, position)`` for a raw int or ISO date value, else None."""
    if not isinstance(raw, str):
        return None
    kind = value_type(raw)
    if kind == "int":
        return "int", int(raw)
    if kind == "date":
        return "date", date.fromisoformat(raw).toordinal()
    return None


def _show(position: int, axis: str) -> str:
    return date.fromordinal(position).isoformat() if axis == "date" else str(position)


def _numeric(value: Any) -> float | None:
    """A float for a numeric JSON value, numeric string or ISO date; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        kind = value_type(value)
        if kind in ("int", "float"):
            return float(value)
        if kind == "date":
            return float(date.fromisoformat(value).toordinal())
    return None


def _label(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _fmt(number: float) -> str:
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _matching(finding: dict[str, Any], traces: Iterable[Trace]) -> Iterator[tuple[str, Trace]]:
    """``(raw sweep value, trace)`` for every trace this finding was computed from."""
    scenario = finding.get("scenario")
    context = {k: str(v) for k, v in (finding.get("context") or {}).items()}
    param = finding["param"]
    for trace in traces:
        if scenario is None:
            if trace.scenario is not None or endpoint_key(trace) != finding["endpoint"]:
                continue
            values = dict(trace.request.params)
        else:
            tag = trace.scenario
            if not tag or tag["name"] != scenario or int(tag["step"]) != finding["step"]:
                continue
            values = {k: str(v) for k, v in (tag.get("vars") or {}).items()}
        raw = values.pop(param, None)
        if raw is not None and values == context:
            yield raw, trace


def _series(finding: dict[str, Any], traces: Iterable[Trace]) -> tuple[str | None, list[tuple[int, Any]]]:
    """The sweep axis and sorted ``(position, field value)`` points for a finding."""
    axis: str | None = None
    rows: dict[int, Any] = {}
    for raw, trace in _matching(finding, traces):
        pos = _position(raw)
        obj = json_object(trace.response)
        if pos is None or obj is None or finding["field"] not in obj:
            continue
        if axis is None:
            axis = pos[0]
        if pos[0] != axis:
            continue
        rows.setdefault(pos[1], obj[finding["field"]])
    return axis, sorted(rows.items())


def _axis_text(x: int, axis: str | None) -> str:
    return _show(x, axis or "int")


def _thin(series: list[tuple[int, Any]], changes: Sequence[int]) -> list[tuple[int, Any]]:
    """At most about MAX_LINE_POINTS points: evenly spaced, plus the ends and the points beside each change."""
    if len(series) <= MAX_LINE_POINTS:
        return series
    stride = -(-len(series) // MAX_LINE_POINTS)
    near = {c + d for c in changes for d in (-1, 0, 1)}
    last = len(series) - 1
    return [pt for i, pt in enumerate(series) if i % stride == 0 or i == last or pt[0] in near]


def _line_chart(finding: dict[str, Any], axis: str, series: list[tuple[int, Any]]) -> str:
    """SVG for step, clamp and linear findings: points over the swept input."""
    changes = _change_positions(finding)
    series = _thin(series, changes)
    xs = [x for x, _ in series] + [c for c in changes]
    xmin, xmax = min(xs), max(xs)
    if xmin == xmax:
        xmax = xmin + 1
    numbers = [_numeric(v) for _, v in series]
    numeric = all(n is not None for n in numbers)
    if numeric:
        values = [float(n) for n in numbers]  # type: ignore[arg-type]
        ymin, ymax = min(values), max(values)
        if ymin == ymax:
            ymin, ymax = ymin - 1, ymax + 1
        y_of = lambda v: (_numeric(v) or 0.0)  # noqa: E731
        top_label, bottom_label = _fmt(ymax), _fmt(ymin)
        ypos = lambda v: TOP + (ymax - y_of(v)) / (ymax - ymin) * (PLOT_H - TOP - BOTTOM)  # noqa: E731
    else:
        levels = sorted({_label(v) for _, v in series})
        index = {name: i for i, name in enumerate(levels)}
        span = max(len(levels) - 1, 1)
        ypos = lambda v: TOP + (len(levels) - 1 - index[_label(v)]) / span * (PLOT_H - TOP - BOTTOM)  # noqa: E731
        top_label, bottom_label = levels[-1], levels[0]

    def px(x: int) -> float:
        return LEFT + (x - xmin) / (xmax - xmin) * (PLOT_W - LEFT - RIGHT)

    bottom = PLOT_H - BOTTOM
    parts = [
        f'<svg viewBox="0 0 {PLOT_W} {PLOT_H}" role="img" aria-label="{_e(finding["field"])} against {_e(finding["param"])}">',
        f'<line class="axis" x1="{LEFT}" y1="{TOP}" x2="{LEFT}" y2="{bottom}"/>',
        f'<line class="axis" x1="{LEFT}" y1="{bottom}" x2="{PLOT_W - RIGHT}" y2="{bottom}"/>',
        f'<text x="{LEFT - 6}" y="{TOP + 4}" text-anchor="end">{_e(top_label)}</text>',
        f'<text x="{LEFT - 6}" y="{bottom}" text-anchor="end">{_e(bottom_label)}</text>',
        f'<text x="{LEFT}" y="{PLOT_H - 10}" text-anchor="start">{_e(_axis_text(xmin, axis))}</text>',
        f'<text x="{PLOT_W - RIGHT}" y="{PLOT_H - 10}" text-anchor="end">{_e(_axis_text(xmax, axis))}</text>',
        f'<text x="{(LEFT + PLOT_W - RIGHT) / 2}" y="{PLOT_H - 10}" text-anchor="middle">{_e(finding["param"])}</text>',
    ]
    for pos, name in zip(changes, _change_names(finding, axis)):
        x = px(pos)
        parts.append(f'<line class="chg" x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{bottom}"/>')
        parts.append(f'<text x="{x + 4:.1f}" y="{TOP + 10}">{_e(name)}</text>')
    points = [(px(x), ypos(v)) for x, v in series]
    if numeric and finding["kind"] in ("linear", "clamp") and len(points) > 1:
        coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        parts.append(f'<polyline class="line" points="{coords}"/>')
    if len(points) <= MAX_DOTS:
        for x, y in points:
            parts.append(f'<circle class="pt" cx="{x:.1f}" cy="{y:.1f}" r="2.4"/>')
    parts.append("</svg>")
    return "".join(parts)


def _change_positions(finding: dict[str, Any]) -> list[int]:
    """Positions where a finding changes: its ``changes_at``, or a clamp's echo end."""
    raws: list[Any] = list(finding.get("changes_at") or [])
    if finding.get("kind") == "clamp" and finding.get("echo_until") is not None:
        raws.append(finding["echo_until"])
    positions = []
    for raw in raws:
        pos = _position(str(raw))
        if pos is not None:
            positions.append(pos[1])
    return positions


def _change_names(finding: dict[str, Any], axis: str) -> list[str]:
    raws: list[Any] = list(finding.get("changes_at") or [])
    if finding.get("kind") == "clamp" and finding.get("echo_until") is not None:
        raws.append(finding["echo_until"])
    names = []
    for raw in raws:
        pos = _position(str(raw))
        if pos is not None:
            names.append(str(raw))
    return names


def _band_chart(finding: dict[str, Any]) -> str:
    """SVG for a shape finding: one coloured band per response-shape segment."""
    segments = finding.get("segments") or []
    spans = []
    for seg in segments:
        lo, hi = _position(str(seg["from"])), _position(str(seg["to"]))
        if lo is None or hi is None:
            return ""
        spans.append((lo[1], hi[1]))
    if not spans:
        return ""
    xmin = min(lo for lo, _ in spans)
    xmax = max(hi for _, hi in spans)
    if xmin == xmax:
        xmax = xmin + 1
    axis = _position(str(segments[0]["from"]))[0]  # type: ignore[index]

    def px(x: int) -> float:
        return LEFT + (x - xmin) / (xmax - xmin) * (PLOT_W - LEFT - RIGHT)

    parts = [f'<svg viewBox="0 0 {PLOT_W} {BAND_H}" role="img" aria-label="response shape against {_e(finding["param"])}">']
    for i, (seg, (lo, hi)) in enumerate(zip(segments, spans)):
        left = px(lo)
        width = max(px(hi) - left, 4.0)
        parts.append(
            f'<rect class="c{i % 6}" x="{left:.1f}" y="{TOP}" width="{width:.1f}" height="{BAND_H - TOP - BOTTOM}" rx="3">'
            f"<title>{_e(seg['value'])}</title></rect>"
        )
    bottom = BAND_H - 10
    parts += [
        f'<text x="{LEFT}" y="{bottom}" text-anchor="start">{_e(_show(xmin, axis))}</text>',
        f'<text x="{PLOT_W - RIGHT}" y="{bottom}" text-anchor="end">{_e(_show(xmax, axis))}</text>',
        "</svg>",
    ]
    legend = "".join(
        f'<li><i class="sw c{i % 6}"></i>{_e(seg["value"])} for {_e(_show(lo, axis))} to {_e(_show(hi, axis))}</li>'
        for i, (seg, (lo, hi)) in enumerate(zip(segments, spans))
    )
    return "".join(parts) + f'<ul class="legend">{legend}</ul>'


def _chart(finding: dict[str, Any], traces: Sequence[Trace]) -> str:
    kind = finding.get("kind")
    if kind == "shape":
        return _band_chart(finding)
    if kind not in ("step", "clamp", "linear"):
        return ""
    axis, series = _series(finding, traces)
    if axis is None or not series:
        return ""
    return _line_chart(finding, axis, series)


def _evidence(claim: dict[str, Any]) -> str:
    if claim.get("mismatches"):
        return "; ".join(claim["mismatches"])
    if claim.get("observed"):
        return f"observed status {claim['observed']['status']}"
    return "no matching trace"


def _badge(text: str, css: str) -> str:
    return f'<span class="badge {css}">{_e(text)}</span>'


def _claims_section(claims: list[dict[str, Any]]) -> str:
    out = ["<h2>Documentation vs reality</h2>"]
    if not claims:
        return "".join(out + ['<p class="muted">No claims supplied.</p>'])
    rows = []
    for claim in claims:
        css = _VERDICT_CLASS.get(claim.get("verdict", ""), "grey")
        rows.append(
            f"<tr><td><code>{_e(claim['id'])}</code></td><td>{_e(claim['text'])}</td>"
            f"<td>{_badge(claim['verdict'], css)}</td><td>{_e(_evidence(claim))}</td></tr>"
        )
    out.append(
        '<div class="table-wrap"><table><thead><tr><th>id</th><th>claim</th><th>verdict</th>'
        "<th>evidence</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    return "".join(out)


def _hidden_section(spec: dict[str, Any], traces: Sequence[Trace]) -> str:
    findings = list(spec.get("boundaries", [])) + list(spec.get("normalization", []))
    out = ["<h2>Hidden behaviour</h2>"]
    if not findings:
        return "".join(out + ['<p class="muted">None detected.</p>'])
    out.append(
        '<p class="note">Points are the samples in each sweep. A change tag is <code>exact</code> when the '
        "samples either side of it are adjacent, or <code>±N</code> when N unsampled inputs could hide the "
        "exact edge.</p>"
    )
    for finding in findings:
        kind = finding.get("kind") or "normalization"
        meta = [str(finding.get("endpoint", ""))]
        if finding.get("param"):
            meta.append(f"parameter {finding['param']}")
        if finding.get("scenario"):
            steps = finding.get("steps") or [finding.get("step")]
            meta.append(f"scenario {finding['scenario']}, step {', '.join(str(s) for s in steps)}")
        if finding.get("contexts", 1) > 1:
            meta.append(f"holds in {finding['contexts']} contexts")
        if "evidence_pairs" in finding:
            meta.append(f"{finding['equivalent_pairs']} of {finding['evidence_pairs']} pairs equivalent")
        if evidence_note(finding):
            meta.append(evidence_note(finding))
        out.append('<article class="panel">')
        out.append(f"<h3>{_badge(kind, 'warn')} {_e(finding['description'])}</h3>")
        out.append(f'<p class="meta">{_e(" · ".join(m for m in meta if m))}</p>')
        out.append(_chart(finding, traces))
        out.append("</article>")
    return "".join(out)


def _gaps_section(spec: dict[str, Any]) -> str:
    out = ["<h2>Thin evidence</h2>"]
    if "evidence_gaps" not in spec:
        return "".join(out + ['<p class="muted">Not computed for this spec; run infer again.</p>'])
    gaps = spec["evidence_gaps"]
    if not gaps:
        return "".join(out + ['<p class="muted">None detected.</p>'])
    rows = [
        f"<tr><td>{_badge(gap['kind'], 'warn')}</td><td>{_e(gap['where'])}</td>"
        f"<td>{_e(gap['detail'])}</td><td>{_e(gap['suggestion'])}</td></tr>"
        for gap in gaps
    ]
    out.append('<p class="note">What the recording never covered. Each row names the workload addition that would close it.</p>')
    out.append(
        '<div class="table-wrap"><table><thead><tr><th>kind</th><th>where</th><th>detail</th>'
        "<th>suggested workload addition</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    return "".join(out)


def _flows_section(spec: dict[str, Any], traces: Sequence[Trace]) -> str:
    flows = spec.get("flows", [])
    out = ["<h2>Identifier flows</h2>"]
    if not flows:
        return "".join(out + ['<p class="muted">None detected.</p>'])
    formats = identifier_formats(flows, traces)
    rows = []
    for flow in flows:
        fmt, distinct = formats.get((flow["from"], flow["field"]), ("no values observed", 0))
        rows.append(
            f"<tr><td><code>{_e(flow['from'])}</code></td><td><code>{_e(flow['field'])}</code></td>"
            f"<td><code>{_e(flow['to'])}</code></td><td>{_e(flow['as'])}</td><td>{flow['count']}</td>"
            f"<td>{_e(fmt)} ({distinct} distinct)</td></tr>"
        )
    out.append(
        '<div class="table-wrap"><table><thead><tr><th>from</th><th>field</th><th>to</th><th>sent as</th>'
        "<th>times</th><th>id format</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    out.append('<p class="note">Replay compares the format of these identifiers, not their exact values.</p>')
    return "".join(out)


def _volatile_section(spec: dict[str, Any]) -> str:
    out = ["<h2>Volatile fields</h2>"]
    volatile = spec.get("volatile_fields", {})
    items = [
        f"<li><code>{_e(key)}</code> <code>{_e(f['path'])}</code>: {_e(f['reason'])}</li>"
        for key, fields in sorted(volatile.items())
        for f in fields
    ]
    if not items:
        return "".join(out + ['<p class="muted">None detected.</p>'])
    return "".join(out + ["<ul>", *items, "</ul>"])


def _replay_section(replay: dict[str, Any]) -> str:
    total = replay.get("total", 0)
    matched = replay.get("matched", 0)
    equivalence = float(replay.get("equivalence", 1.0))
    verdict = "EQUIVALENT" if equivalence >= 1.0 else "NOT EQUIVALENT"
    out = [
        "<h2>Replay</h2>",
        '<div class="panel">',
        f"<p>{_badge(verdict, 'ok' if equivalence >= 1.0 else 'bad')} "
        f"<strong>{matched} / {total} responses equivalent ({equivalence * 100:.1f}%)</strong></p>",
    ]
    if "remapped" in replay:
        out.append(f"<p>Identifiers remapped: {replay['remapped']} requests used the rebuild's own identifiers.</p>")
    out.append("</div>")
    mismatches = replay.get("mismatches", [])
    if mismatches:
        out.append(f"<h3>Divergences ({len(mismatches)})</h3>")
        for item in mismatches[:MAX_LISTED]:
            diffs = "".join(f"<li>{_e(d)}</li>" for d in item.get("differences", []))
            out.append(
                f'<article class="panel"><p><code>{_e(item.get("trace_id", ""))}</code> '
                f"<code>{_e(item.get('request', ''))}</code></p><ul>{diffs}</ul></article>"
            )
        if len(mismatches) > MAX_LISTED:
            out.append(f'<p class="muted">... and {len(mismatches) - MAX_LISTED} more</p>')
    return "".join(out)


def _probe_section(probe: dict[str, Any]) -> str:
    total = probe.get("total", 0)
    divergences = probe.get("divergences", [])
    agreement = float(probe.get("agreement", 1.0))
    verdict = "AGREES" if not divergences else "DIVERGES"
    out = [
        "<h2>Probe</h2>",
        '<div class="panel">',
        f"<p>{_badge(verdict, 'ok' if not divergences else 'bad')} "
        f"<strong>{total - len(divergences)} / {total} fresh exchanges agree ({agreement * 100:.1f}%)</strong></p>",
        f"<p>{probe.get('scenario_steps', 0)} of them were scenario steps.</p>",
        "</div>",
    ]
    if divergences:
        out.append(f"<h3>Divergences ({len(divergences)})</h3>")
        for item in divergences[:MAX_LISTED]:
            diffs = "".join(f"<li>{_e(d)}</li>" for d in item.get("differences", []))
            where = item.get("query_or_body", "")
            scenario = item.get("scenario")
            name = f" (scenario {scenario['name']}, step {scenario['step']})" if scenario else ""
            out.append(
                f'<article class="panel"><p><code>{_e(item.get("request", ""))}</code> '
                f"<code>{_e(where)}</code>{_e(name)}</p><ul>{diffs}</ul></article>"
            )
        if len(divergences) > MAX_LISTED:
            out.append(f'<p class="muted">... and {len(divergences) - MAX_LISTED} more</p>')
    return "".join(out)


def _header(spec: dict[str, Any], title: str, replay: dict[str, Any] | None, probe: dict[str, Any] | None) -> str:
    badges = []
    if replay is not None:
        eq = float(replay.get("equivalence", 1.0))
        badges.append(_badge(f"Replay {eq * 100:.1f}% equivalent", "ok" if eq >= 1.0 else "bad"))
    if probe is not None:
        clean = not probe.get("divergences")
        badges.append(_badge("Probe " + ("agrees" if clean else f"{len(probe['divergences'])} divergences"),
                             "ok" if clean else "bad"))
    return (
        '<header class="top">'
        f"<h1>{_e(title)}</h1>"
        f'<p class="muted">{spec.get("trace_count", 0)} recorded exchanges · '
        f"{len(spec.get('endpoints', []))} endpoints · "
        f"{len(spec.get('boundaries', [])) + len(spec.get('normalization', []))} hidden behaviours</p>"
        f'<div class="badges">{"".join(badges)}</div>'
        "</header>"
    )


def render_html_report(
    spec: dict[str, Any],
    traces: Iterable[Trace],
    title: str = "Evidence report",
    replay: dict[str, Any] | None = None,
    probe: dict[str, Any] | None = None,
) -> str:
    """Render the evidence report as one self-contained HTML page.

    ``replay`` and ``probe`` are the JSON summaries written by
    ``archaeology replay --json`` and ``archaeology probe --json``.
    """
    traces = list(traces)
    body = [
        _header(spec, title, replay, probe),
        _claims_section(spec.get("claims", [])),
        _hidden_section(spec, traces),
        _gaps_section(spec),
        _flows_section(spec, traces),
        _volatile_section(spec),
    ]
    if replay is not None:
        body.append(_replay_section(replay))
    if probe is not None:
        body.append(_probe_section(probe))
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>\n"
        "<body><main>" + "".join(body) + "</main></body></html>\n"
    )
