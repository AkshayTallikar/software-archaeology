"""Serve an in-process WSGI app over HTTP: ``python -m archaeology.serve module:attr``.

Listens on ``$HOST:$PORT`` (default ``127.0.0.1:8000``). Used to put a Python
system behind a real socket, for example as a ``cmd:`` process target or as
the upstream of the recording proxy.
"""

from __future__ import annotations

import os
import sys
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .cli import _resolve_wsgi_app


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m archaeology.serve module:attr", file=sys.stderr)
        return 2
    app = _resolve_wsgi_app(args[0])
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    with make_server(host, port, app, handler_class=_QuietHandler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
