"""Command line interface: ``archaeology record | proxy | infer | brief | replay | probe | report | demo``."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .brief import generate_brief
from .html_report import render_html_report
from .infer.claims import load_claims
from .infer.spec import infer_spec, load_spec, render_report, save_spec, volatile_paths_for
from .probe import generate_requests, merge_reports, probe, probe_scenarios
from .probe import render_markdown as render_probe
from .probe import to_dict as probe_to_dict
from .recorder import JsonlSink, RecordingProxy, load_workload, record, record_target
from .replay import load_target, render_markdown, replay, to_dict
from .scenarios import sample_scenarios
from .trace import read_traces


def _import(spec: str) -> Any:
    module_name, _, attr = spec.partition(":")
    obj = importlib.import_module(module_name)
    for part in (attr or "application").split("."):
        obj = getattr(obj, part)
    return obj


def _resolve_wsgi_app(spec: str) -> Any:
    """``module:attr``; a zero-argument ``make_*`` factory is called for fresh state."""
    obj = _import(spec)
    if getattr(obj, "__name__", "").startswith("make_"):
        obj = obj()
    return obj


def cmd_record(args: argparse.Namespace) -> int:
    workload = load_workload(args.workload)
    if args.target:
        target = load_target(args.target)
        try:
            count = record_target(target, workload, args.out)
        finally:
            close = getattr(target, "close", None)
            if callable(close):
                close()
    else:
        count = record(_resolve_wsgi_app(args.app), workload, args.out)
    print(f"Recorded {count} exchanges to {args.out}")
    return 0


def _parse_listen(listen: str) -> tuple[str, int]:
    """``HOST:PORT`` into its parts; port 0 asks the OS for a free port."""
    host, sep, port = listen.rpartition(":")
    if not sep or not host:
        raise ValueError(f"--listen must be HOST:PORT, got {listen!r}")
    return host, int(port)


def start_recording_proxy(
    upstream: str, listen: str, out: str | Path, append: bool = False
) -> tuple[RecordingProxy, JsonlSink]:
    """Start a :class:`RecordingProxy` writing to a JSON Lines file. Returns ``(proxy, sink)``.

    The caller stops the proxy and closes the sink. This does not block, so
    tests can drive the proxy directly.
    """
    host, port = _parse_listen(listen)
    sink = JsonlSink(out, append=append)
    try:
        proxy = RecordingProxy(upstream, sink, host=host, port=port).start()
    except Exception:
        sink.close()
        raise
    return proxy, sink


def _wait_forever() -> None:
    """Block the main thread until interrupted."""
    while True:
        time.sleep(3600)


def cmd_proxy(args: argparse.Namespace) -> int:
    proxy, sink = start_recording_proxy(args.upstream, args.listen, args.out, args.append)
    print(f"Recording proxy listening on {proxy.url}, forwarding to {args.upstream}, recording to {args.out}")
    try:
        _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()
        sink.close()
    print(f"Stopped. Recorded {proxy.count} exchanges to {args.out}")
    return 0


def cmd_infer(args: argparse.Namespace) -> int:
    traces = read_traces(args.traces)
    claims = load_claims(args.claims) if args.claims else None
    spec = infer_spec(traces, claims)
    save_spec(spec, args.spec)
    Path(args.report).write_text(render_report(spec), encoding="utf-8")
    contradicted = sum(1 for c in spec["claims"] if c["verdict"] == "CONTRADICTED")
    hidden = len(spec["boundaries"]) + len(spec["normalization"])
    print(
        f"Inferred {len(spec['endpoints'])} endpoints and {hidden} hidden behaviours from {len(traces)} traces; "
        f"{contradicted} documented claims contradicted. Spec: {args.spec}  Report: {args.report}"
    )
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    brief = generate_brief(load_spec(args.spec), read_traces(args.traces), system_name=args.name)
    Path(args.out).write_text(brief, encoding="utf-8")
    print(f"Wrote rebuild brief to {args.out}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    replay_summary = json.loads(Path(args.replay).read_text(encoding="utf-8")) if args.replay else None
    probe_summary = json.loads(Path(args.probe).read_text(encoding="utf-8")) if args.probe else None
    page = render_html_report(
        load_spec(args.spec), read_traces(args.traces),
        title=args.title, replay=replay_summary, probe=probe_summary,
    )
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"Wrote evidence report to {args.out} ({len(page.encode('utf-8')) // 1024} KB)")
    return 0


def _run_replay(traces_path: str, target_spec: str, spec_path: str | None, threshold: float,
                report_path: str | None, json_path: str | None) -> int:
    traces = read_traces(traces_path)
    target = load_target(target_spec)
    ignore = None
    if spec_path:
        spec = load_spec(spec_path)
        ignore = lambda request: volatile_paths_for(spec, request)  # noqa: E731
    try:
        report = replay(traces, target, ignore=ignore)
    finally:
        close = getattr(target, "close", None)
        if callable(close):
            close()
    markdown = render_markdown(report)
    if report_path:
        Path(report_path).write_text(markdown, encoding="utf-8")
    if json_path:
        Path(json_path).write_text(json.dumps(to_dict(report), indent=2), encoding="utf-8")
    print(markdown.split("\n## ")[0].strip())
    return 0 if report.passed(threshold) else 1


def cmd_replay(args: argparse.Namespace) -> int:
    return _run_replay(args.traces, args.target, args.spec, args.threshold, args.report, args.json)


def _run_probe(reference: str, candidate: str, spec_path: str, traces_path: str,
               count: int, seed: int, report_path: str | None,
               workload_path: str | None = None, scenario_count: int = 0,
               json_path: str | None = None) -> int:
    """Probe two systems with fresh requests and, given a workload, fresh scenario runs.

    Only stateless traces seed the fresh requests. Scenario runs are sampled
    from ``workload_path`` and probed on the same two targets; their steps are
    merged into the same report.
    """
    spec = load_spec(spec_path)
    traces = read_traces(traces_path)
    requests = generate_requests(spec, [t for t in traces if t.scenario is None], count, seed=seed)
    runs = sample_scenarios(load_workload(workload_path), scenario_count, seed=seed) if workload_path else []
    targets = [load_target(reference), load_target(candidate)]
    try:
        report = probe(targets[0], targets[1], requests, spec)
        if runs:
            report = merge_reports(report, probe_scenarios(targets[0], targets[1], runs, spec))
    finally:
        for target in targets:
            close = getattr(target, "close", None)
            if callable(close):
                close()
    markdown = render_probe(report)
    if report_path:
        Path(report_path).write_text(markdown, encoding="utf-8")
    if json_path:
        Path(json_path).write_text(json.dumps(probe_to_dict(report), indent=2), encoding="utf-8")
    print(markdown.split("\n## ")[0].strip())
    return 0 if not report.divergences else 1


def cmd_probe(args: argparse.Namespace) -> int:
    return _run_probe(args.reference, args.candidate, args.spec, args.traces, args.count, args.seed,
                      args.report, workload_path=args.workload, scenario_count=args.scenario_count,
                      json_path=args.json)


def cmd_demo(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    traces, spec, report, brief = (out / n for n in ("traces.jsonl", "spec.json", "report.md", "brief.md"))
    print("== 1. Excavate: recording the legacy system ==")
    cmd_record(argparse.Namespace(app="legacy_app.billing:make_app", target=None,
                                  workload="legacy_app/workload.json", out=str(traces)))
    print("\n== 2. Interpret: inferring its true behaviour ==")
    cmd_infer(argparse.Namespace(traces=str(traces), claims="legacy_app/claims.json", spec=str(spec), report=str(report)))
    print("\n== 3. Rebuild: writing the brief for coding agents ==")
    cmd_brief(argparse.Namespace(spec=str(spec), traces=str(traces), out=str(brief), name="ACME Billing"))
    print(f"\n== 4. Prove: replaying recorded traffic against {args.target} ==")
    replay_status = _run_replay(str(traces), args.target, str(spec), 1.0,
                                str(out / "equivalence.md"), str(out / "equivalence.json"))
    print(f"\n== 5. Probe: {args.probe_count} fresh requests the recording never contained ==")
    probe_status = _run_probe("legacy_app.billing:make_app", args.target, str(spec), str(traces),
                              args.probe_count, 0, str(out / "probe.md"), json_path=str(out / "probe.json"))
    print("\n== 6. Report: writing the evidence report ==")
    cmd_report(argparse.Namespace(spec=str(spec), traces=str(traces), replay=str(out / "equivalence.json"),
                                  probe=str(out / "probe.json"), title="ACME Billing",
                                  out=str(out / "report.html")))
    return replay_status or probe_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archaeology", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="drive a system with a workload and record traces")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--app", help="module:attr of an in-process WSGI app or make_* factory")
    source.add_argument(
        "--target",
        help="any target spec: module:attr (WSGI or ASGI), cmd:<command> or an http(s) base URL",
    )
    p.add_argument("--workload", required=True)
    p.add_argument("--out", default="traces.jsonl")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("proxy", help="record the real traffic of a live system passing through a proxy")
    p.add_argument("--upstream", required=True, help="base URL of the live system")
    p.add_argument("--listen", default="127.0.0.1:8080", help="HOST:PORT to listen on")
    p.add_argument("--out", default="traces.jsonl")
    p.add_argument("--append", action="store_true", help="append to --out instead of overwriting it")
    p.set_defaults(func=cmd_proxy)

    p = sub.add_parser("infer", help="infer the system's true behaviour from traces")
    p.add_argument("--traces", required=True)
    p.add_argument("--claims", help="documented claims to test against the evidence")
    p.add_argument("--spec", default="spec.json")
    p.add_argument("--report", default="report.md")
    p.set_defaults(func=cmd_infer)

    p = sub.add_parser("brief", help="write the rebuild brief for coding agents")
    p.add_argument("--spec", required=True)
    p.add_argument("--traces", required=True)
    p.add_argument("--out", default="brief.md")
    p.add_argument("--name", default="the legacy system")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("replay", help="replay traces against a rebuild and check equivalence")
    p.add_argument("--traces", required=True)
    p.add_argument("--target", required=True, help="module:attr (WSGI or ASGI app) or http(s) base URL")
    p.add_argument("--spec", help="spec.json, to ignore volatile fields")
    p.add_argument("--threshold", type=float, default=1.0)
    p.add_argument("--report", help="write the markdown report here")
    p.add_argument("--json", help="write the JSON report here")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("probe", help="send fresh generated requests to two systems and diff them")
    p.add_argument("--reference", required=True, help="the legacy system: module:attr or URL")
    p.add_argument("--candidate", required=True, help="the rebuild: module:attr or URL")
    p.add_argument("--spec", required=True)
    p.add_argument("--traces", required=True, help="recorded traces, for observed parameter values")
    p.add_argument("--count", type=int, default=3000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--workload", help="workload with scenarios to probe as fresh multi-step runs")
    p.add_argument("--scenario-count", type=int, default=300,
                   help="how many scenario runs to sample from --workload")
    p.add_argument("--report", help="write the markdown report here")
    p.add_argument("--json", help="write the JSON report here")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("report", help="write a self-contained HTML evidence report")
    p.add_argument("--spec", required=True)
    p.add_argument("--traces", required=True)
    p.add_argument("--replay", help="replay JSON (archaeology replay --json) to show")
    p.add_argument("--probe", help="probe JSON (archaeology probe --json) to show")
    p.add_argument("--title", default="Evidence report")
    p.add_argument("--out", default="report.html")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("demo", help="run the whole pipeline on the bundled ACME Billing system")
    p.add_argument("--out", default="out")
    p.add_argument("--target", default="modern_app.main:make_app")
    p.add_argument("--probe-count", type=int, default=3000)
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
