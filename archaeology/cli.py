"""Command line interface: ``archaeology record | explore | proxy | infer | brief | replay | mutate | probe | report | demo``."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .brief import generate_brief
from .explore import explore_target, render_log
from . import mutate as mutation
from .html_report import render_html_report
from .infer.claims import load_claims
from .infer.spec import infer_spec, load_spec, render_report, save_spec, volatile_paths_for
from .probe import generate_requests, merge_reports, probe, probe_scenarios
from .probe import render_markdown as render_probe
from .probe import to_dict as probe_to_dict
from .recorder import JsonlSink, RecordingProxy, load_workload, record, record_target
from .replay import load_target, render_markdown, replay, to_dict
from .scenarios import sample_scenarios
from .trace import read_traces, write_traces
from .wsgi import call_wsgi


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


def cmd_explore(args: argparse.Namespace) -> int:
    """Record a workload's behaviour with adaptive sampling, writing traces and an exploration log."""
    workload = load_workload(args.workload)
    if args.target:
        target = load_target(args.target)
    else:
        app = _resolve_wsgi_app(args.app)
        target = lambda request: call_wsgi(app, request)  # noqa: E731
    try:
        result = explore_target(target, workload)
    finally:
        close = getattr(target, "close", None)
        if callable(close):
            close()
    write_traces(args.out, result.traces)
    Path(args.log).write_text(render_log(result), encoding="utf-8")
    ratio = result.full / len(result.traces) if result.traces else float("inf")
    print(
        f"Explored {len(result.traces)} exchanges (full recording: {result.full}, {ratio:.2f}x fewer) "
        f"to {args.out}  Log: {args.log}"
    )
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
        f"{contradicted} documented claims contradicted; {len(spec['evidence_gaps'])} evidence gaps. "
        f"Spec: {args.spec}  Report: {args.report}"
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


def cmd_mutate(args: argparse.Namespace) -> int:
    """Score how well the recording pins the rebuild: replay it against every one-change mutant."""
    run = mutation.run_mutation(
        args.rebuild,
        args.traces,
        spec=args.spec,
        module=args.module,
        max_mutants=args.max_mutants,
        timeout=args.timeout,
        jobs=args.jobs,
    )
    Path(args.out).write_text(mutation.render_markdown(run), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(mutation.to_dict(run), indent=2) + "\n", encoding="utf-8")
    summary = run.summary()
    score = "n/a" if summary["score"] is None else f"{summary['score'] * 100:.1f}%"
    print(
        f"Mutation score {score}: killed {summary['killed']}, survived {summary['survived']}, "
        f"errors {summary['errors']} of {run.generated} mutants generated; "
        f"wall time {run.wall_seconds:.1f} s. Report: {args.out}"
    )
    return 0


@dataclass(frozen=True)
class DemoSystem:
    """A bundled legacy system and its rebuild, runnable end to end by ``demo``."""

    title: str
    reference: str
    workload: str
    claims: str
    rebuild: str
    stateful: bool


DEMO_SYSTEMS: dict[str, DemoSystem] = {
    "billing": DemoSystem(
        title="ACME Billing",
        reference="legacy_app.billing:make_app",
        workload="legacy_app/workload.json",
        claims="legacy_app/claims.json",
        rebuild="modern_app.main:make_app",
        stateful=False,
    ),
    "library": DemoSystem(
        title="Pinecrest Library",
        reference="cmd:node legacy_library/server.js",
        workload="legacy_library/workload.json",
        claims="legacy_library/claims.json",
        rebuild="modern_library.main:make_app",
        stateful=True,
    ),
}


def cmd_demo(args: argparse.Namespace) -> int:
    system = DEMO_SYSTEMS[args.system]
    target = args.target or system.rebuild
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    traces, spec, report, brief = (out / n for n in ("traces.jsonl", "spec.json", "report.md", "brief.md"))
    print(f"== 1. Excavate: recording {system.title} ({system.reference}) ==")
    cmd_record(argparse.Namespace(app=None, target=system.reference, workload=system.workload, out=str(traces)))
    print("\n== 2. Interpret: inferring its true behaviour ==")
    cmd_infer(argparse.Namespace(traces=str(traces), claims=system.claims, spec=str(spec), report=str(report)))
    print("\n== 3. Rebuild: writing the brief for coding agents ==")
    cmd_brief(argparse.Namespace(spec=str(spec), traces=str(traces), out=str(brief), name=system.title))
    print(f"\n== 4. Prove: replaying recorded traffic against {target} ==")
    replay_status = _run_replay(str(traces), target, str(spec), 1.0,
                                str(out / "equivalence.md"), str(out / "equivalence.json"))
    scenarios = args.scenario_count if system.stateful else 0
    extra = f" and {scenarios} fresh scenario runs" if scenarios else ""
    print(f"\n== 5. Probe: {args.probe_count} fresh requests{extra} the recording never contained ==")
    probe_status = _run_probe(system.reference, target, str(spec), str(traces),
                              args.probe_count, 0, str(out / "probe.md"),
                              workload_path=system.workload if scenarios else None,
                              scenario_count=scenarios, json_path=str(out / "probe.json"))
    print("\n== 6. Report: writing the evidence report ==")
    cmd_report(argparse.Namespace(spec=str(spec), traces=str(traces), replay=str(out / "equivalence.json"),
                                  probe=str(out / "probe.json"), title=system.title,
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

    p = sub.add_parser("explore", help="record a workload with adaptive sampling along its ranges")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--app", help="module:attr of an in-process WSGI app or make_* factory")
    source.add_argument(
        "--target",
        help="any target spec: module:attr (WSGI or ASGI), cmd:<command> or an http(s) base URL",
    )
    p.add_argument("--workload", required=True)
    p.add_argument("--out", default="traces.jsonl")
    p.add_argument("--log", default="explore.md", help="where to write the exploration log")
    p.set_defaults(func=cmd_explore)

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

    p = sub.add_parser("mutate", help="score how well the recording pins a rebuild (mutation testing)")
    p.add_argument("--rebuild", required=True, help="path to the rebuild's module, e.g. modern_app/main.py")
    p.add_argument("--module", help="import name of the rebuild (default: derived from --rebuild)")
    p.add_argument("--traces", required=True)
    p.add_argument("--spec", required=True, help="spec.json, to ignore volatile fields")
    p.add_argument("--out", default="mutation.md", help="where to write the markdown report")
    p.add_argument("--json", help="write the JSON report here")
    p.add_argument("--max-mutants", type=int, help="score a seeded sample of this many mutants")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds allowed per mutant")
    p.add_argument("--jobs", type=int, help="mutants scored in parallel (default: CPU count)")
    p.set_defaults(func=cmd_mutate)

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

    p = sub.add_parser("demo", help="run the whole pipeline on a bundled legacy system and its rebuild")
    p.add_argument("--system", choices=sorted(DEMO_SYSTEMS), default="billing",
                   help="billing (Python WSGI) or library (Node.js, stateful; needs node)")
    p.add_argument("--out", default="out")
    p.add_argument("--target", help="the rebuild to prove (default: the bundled rebuild)")
    p.add_argument("--probe-count", type=int, default=3000)
    p.add_argument("--scenario-count", type=int, default=300,
                   help="fresh scenario runs to probe on a stateful system")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
