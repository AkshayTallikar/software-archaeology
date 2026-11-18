"""Command line interface: ``archaeology record | infer | brief | replay | demo``."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .brief import generate_brief
from .infer.claims import load_claims
from .infer.spec import infer_spec, load_spec, render_report, save_spec, volatile_paths_for
from .recorder import load_workload, record
from .replay import load_target, render_markdown, replay, to_dict
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
    app = _resolve_wsgi_app(args.app)
    count = record(app, load_workload(args.workload), args.out)
    print(f"Recorded {count} exchanges to {args.out}")
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


def cmd_demo(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    traces, spec, report, brief = (out / n for n in ("traces.jsonl", "spec.json", "report.md", "brief.md"))
    print("== 1. Excavate: recording the legacy system ==")
    cmd_record(argparse.Namespace(app="legacy_app.billing:make_app", workload="legacy_app/workload.json", out=str(traces)))
    print("\n== 2. Interpret: inferring its true behaviour ==")
    cmd_infer(argparse.Namespace(traces=str(traces), claims="legacy_app/claims.json", spec=str(spec), report=str(report)))
    print("\n== 3. Rebuild: writing the brief for coding agents ==")
    cmd_brief(argparse.Namespace(spec=str(spec), traces=str(traces), out=str(brief), name="ACME Billing"))
    print(f"\n== 4. Prove: replaying recorded traffic against {args.target} ==")
    return _run_replay(str(traces), args.target, str(spec), 1.0, str(out / "equivalence.md"), str(out / "equivalence.json"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archaeology", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="drive a WSGI app with a workload and record traces")
    p.add_argument("--app", required=True, help="module:attr of a WSGI app or make_* factory")
    p.add_argument("--workload", required=True)
    p.add_argument("--out", default="traces.jsonl")
    p.set_defaults(func=cmd_record)

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

    p = sub.add_parser("demo", help="run the whole pipeline on the bundled ACME Billing system")
    p.add_argument("--out", default="out")
    p.add_argument("--target", default="modern_app.main:make_app")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
