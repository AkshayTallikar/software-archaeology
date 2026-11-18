"""Call a WSGI application in-process with a :class:`~archaeology.trace.Request`."""

from __future__ import annotations

import io
from typing import Any, Callable
from wsgiref.util import setup_testing_defaults

from .trace import Request, Response

WSGIApp = Callable[..., Any]


def build_environ(request: Request) -> dict[str, Any]:
    body = request.body.encode("utf-8")
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": request.method,
            "PATH_INFO": request.path,
            "QUERY_STRING": request.query_string,
            "CONTENT_TYPE": request.headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
    )
    return environ


def call_wsgi(app: WSGIApp, request: Request) -> Response:
    """Run one request through a WSGI app and capture the response."""
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info=None):
        captured["status"] = int(status.split(" ", 1)[0])
        captured["headers"] = {k.lower(): v for k, v in headers}
        return lambda data: None

    result = app(build_environ(request), start_response)
    try:
        body = b"".join(result)
    finally:
        if hasattr(result, "close"):
            result.close()
    return Response(
        status=captured["status"],
        headers=captured["headers"],
        body=body.decode("utf-8", errors="replace"),
    )
