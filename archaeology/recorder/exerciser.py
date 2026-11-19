"""Drive a WSGI app with a declarative workload so its behaviour gets recorded.

A workload is a JSON object with three optional keys: ``requests`` (explicit
requests, sent in order), ``sweeps`` (parameter sweeps expanded into one
request per combination of varied values), ``scenarios`` (multi-step runs
whose later steps use values captured from earlier responses; see
:mod:`archaeology.scenarios`) and ``interleave`` (several scenarios' steps in
shared global orders; see :mod:`archaeology.interleave`). They are sent in that
order.

The exerciser drives any target: a ``Request -> Response`` callable such as
:class:`~archaeology.replay.WsgiTarget`, ``AsgiTarget``, ``HttpTarget`` or
``ProcessTarget``. :func:`exercise` and :func:`record` keep the original
in-process WSGI interface.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from datetime import timezone

from ..trace import FORM_CONTENT_TYPE, Request, Response, Trace
from ..wsgi import call_wsgi
from .middleware import JsonlSink

Target = Callable[[Request], Response]


def load_workload(path: str | Path) -> dict[str, Any]:
    """Load a workload JSON file."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def request_from_spec(spec: dict[str, Any]) -> Request:
    """Build a :class:`Request` from one explicit request spec."""
    method = spec.get("method", "GET")
    headers: dict[str, str] = {}
    body = ""
    if "form" in spec:
        body = urlencode(spec["form"])
        headers = {"Content-Type": FORM_CONTENT_TYPE}
    if "raw_form" in spec:
        # A form body sent verbatim, for repeated keys that a dict cannot hold.
        body = spec["raw_form"]
        headers = {"Content-Type": FORM_CONTENT_TYPE}
    if "raw_query" in spec:
        query_string = spec["raw_query"]
    else:
        query_string = urlencode(spec.get("query", {}))
    return Request.build(
        method=method,
        path=spec["path"],
        query_string=query_string,
        body=body,
        headers=headers,
    )


def _vary_values(name: str, spec: dict[str, Any]) -> list[str | None]:
    """Expand one vary entry into the values to send (``None`` means omit)."""
    from ..scenarios import vary_values

    return vary_values(name, spec)


def expand_sweep(sweep: dict[str, Any]) -> list[Request]:
    """Expand one sweep into the cartesian product of its varied parameters.

    Varied parameters go into ``form`` when the sweep has a ``form`` key, and
    into ``query`` otherwise. Base parameters come first, then varied ones in
    vary-key order; a varied value overrides a base parameter of the same
    name, and a ``None`` value omits the parameter entirely.
    """
    in_form = "form" in sweep
    base: dict[str, Any] = dict(sweep.get("form", {}) if in_form else sweep.get("query", {}))
    vary: dict[str, Any] = sweep.get("vary", {})

    names = list(vary.keys())
    value_lists = [_vary_values(name, vary[name]) for name in names]

    requests: list[Request] = []
    for combo in itertools.product(*value_lists):
        varied = dict(zip(names, combo))
        params: dict[str, Any] = {k: v for k, v in base.items() if k not in varied}
        for name in names:
            value = varied[name]
            if value is not None:
                params[name] = value
        spec: dict[str, Any] = {"method": sweep.get("method", "GET"), "path": sweep["path"]}
        spec["form" if in_form else "query"] = params
        requests.append(request_from_spec(spec))
    return requests


def expand_workload(workload: dict[str, Any]) -> list[Request]:
    """Expand a whole workload: explicit requests first, then each sweep in order."""
    requests = [request_from_spec(spec) for spec in workload.get("requests", [])]
    for sweep in workload.get("sweeps", []):
        requests.extend(expand_sweep(sweep))
    return requests


def exercise_target(
    target: Target,
    workload: dict[str, Any],
    sink: Callable[[Trace], None],
    clock: Callable[[], datetime] | None = None,
    id_prefix: str = "t-",
) -> int:
    """Send every workload request and scenario step to ``target`` and record each exchange.

    Returns the number of exchanges recorded.
    """
    from ..interleave import expand_interleavings, run_interleaved
    from ..scenarios import expand_scenarios, run_scenario

    now = clock or (lambda: datetime.now(timezone.utc))
    count = 0

    def emit(request: Request, response: Response, scenario: dict[str, Any] | None) -> None:
        nonlocal count
        count += 1
        sink(Trace(f"{id_prefix}{count:06d}", now().isoformat(), request, response, scenario))

    for request in expand_workload(workload):
        emit(request, target(request), None)
    runs = expand_scenarios(workload)
    for run in runs:
        run_scenario(run, target, on_exchange=emit)
    for interleaved in expand_interleavings(workload, first_run=len(runs)):
        run_interleaved(interleaved, target, on_exchange=emit)
    return count


def record_target(
    target: Target,
    workload: dict[str, Any],
    out_path: str | Path,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Exercise ``target`` and write the traces to a JSON Lines file. Returns the count."""
    with JsonlSink(out_path) as sink:
        return exercise_target(target, workload, sink, clock)


def exercise(
    app: Callable[..., Any],
    workload: dict[str, Any],
    sink: Callable[..., None],
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Exercise an in-process WSGI ``app``. Returns the number of exchanges recorded."""
    return exercise_target(lambda request: call_wsgi(app, request), workload, sink, clock)


def record(
    app: Callable[..., Any],
    workload: dict[str, Any],
    out_path: str | Path,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Exercise a WSGI ``app`` and write the traces to a JSON Lines file. Returns the count."""
    with JsonlSink(out_path) as sink:
        return exercise(app, workload, sink, clock)
