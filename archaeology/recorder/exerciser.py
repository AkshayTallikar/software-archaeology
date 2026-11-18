"""Drive a WSGI app with a declarative workload so its behaviour gets recorded.

A workload is a JSON object with two optional keys: ``requests`` (explicit
requests, sent in order) and ``sweeps`` (parameter sweeps expanded into one
request per combination of varied values).
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from ..trace import FORM_CONTENT_TYPE, Request
from ..wsgi import call_wsgi
from .middleware import JsonlSink, RecordingMiddleware


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


def _range_values(spec: dict[str, Any]) -> list[str]:
    """Expand ``{"range": [start, stop_inclusive, step]}`` into string values."""
    start, stop, step = spec["range"]
    if step > 0:
        stop_excl = stop + 1
    elif step < 0:
        stop_excl = stop - 1
    else:
        raise ValueError("range step must not be zero")
    return [str(i) for i in range(start, stop_excl, step)]


def _vary_values(name: str, spec: dict[str, Any]) -> list[str | None]:
    """Expand one vary entry into the values to send (``None`` means omit)."""
    if "range" in spec:
        return _range_values(spec)
    if "values" in spec:
        return [None if v is None else str(v) for v in spec["values"]]
    raise ValueError(f"vary parameter {name!r} needs 'range' or 'values'")


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


def exercise(
    app: Callable[..., Any],
    workload: dict[str, Any],
    sink: Callable[..., None],
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Send every workload request through a recording wrapper of ``app``.

    Returns the number of requests sent.
    """
    wrapped = RecordingMiddleware(app, sink, clock)
    requests = expand_workload(workload)
    for request in requests:
        call_wsgi(wrapped, request)
    return len(requests)


def record(
    app: Callable[..., Any],
    workload: dict[str, Any],
    out_path: str | Path,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Exercise ``app`` and write the traces to a JSON Lines file. Returns the count."""
    with JsonlSink(out_path) as sink:
        return exercise(app, workload, sink, clock)
