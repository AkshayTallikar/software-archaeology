"""A recording reverse proxy: sits in front of a live system and records its real traffic.

:class:`RecordingProxy` listens on a local port, forwards every request it
receives to an upstream base URL unchanged, hands the upstream's answer back
to the client and records the exchange as one :class:`~archaeology.trace.Trace`.
The proxy never alters a body or a status: what a client sees is exactly what
the legacy system answered, and what is recorded is the same exchange.

Hop-by-hop headers (RFC 7230 section 6.1) are never forwarded. Upstream
bodies are decoded by the HTTP client, so ``content-encoding`` and
``content-length`` are not sent back; the proxy sets a correct
``Content-Length`` for the body it returns.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import httpx

from ..trace import Request, Response, Trace

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Request headers never sent upstream. Host names the proxy, not the upstream;
# content-length is recomputed by the HTTP client from the body bytes; and
# accept-encoding is left to the client's default, so the upstream answers only
# in encodings the client decodes.
_NOT_FORWARDED_REQUEST_HEADERS = HOP_BY_HOP_HEADERS | {"host", "content-length", "accept-encoding"}

# Response headers never sent back to the client (see the module docstring).
_NOT_RETURNED_RESPONSE_HEADERS = HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}

_UPSTREAM_UNAVAILABLE_BODY = json.dumps({"error": "UPSTREAM_UNAVAILABLE"}).encode("utf-8")


class _ProxyServer(ThreadingHTTPServer):
    """The threading HTTP server, carrying a reference back to its proxy."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], proxy: "RecordingProxy") -> None:
        self.proxy = proxy
        super().__init__(address, _ProxyHandler)


class _ProxyHandler(BaseHTTPRequestHandler):
    """Forward one request upstream, record the exchange and answer the client."""

    server: _ProxyServer

    def _forward(self) -> None:
        proxy = self.server.proxy
        path, _, query_string = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        content_type = self.headers.get("Content-Type")

        url = f"{proxy.upstream}{path}"
        if query_string:
            url = f"{url}?{query_string}"
        forwarded = {
            k.lower(): v for k, v in self.headers.items() if k.lower() not in _NOT_FORWARDED_REQUEST_HEADERS
        }
        try:
            upstream = proxy.client.request(self.command, url, headers=forwarded, content=body or None)
        except httpx.TransportError:
            # Unreachable or broken upstream: answer, but there is no exchange to record.
            self._send(502, [("Content-Type", "application/json")], _UPSTREAM_UNAVAILABLE_BODY)
            return

        response_body = upstream.content
        proxy.record(
            Request.build(
                method=self.command,
                path=path,
                query_string=query_string,
                body=body.decode("utf-8", errors="replace"),
                headers={"content-type": content_type} if content_type else None,
            ),
            Response(
                status=upstream.status_code,
                headers={
                    k: v for k, v in ((k.lower(), v) for k, v in upstream.headers.items())
                    if k not in HOP_BY_HOP_HEADERS
                },
                body=response_body.decode("utf-8", errors="replace"),
            ),
        )
        self._send(upstream.status_code, upstream.headers.multi_items(), response_body)

    def _send(self, status: int, pairs: list[tuple[str, str]], body: bytes) -> None:
        """Write a complete response. ``pairs`` keeps repeated headers such as Set-Cookie."""
        self.send_response(status)
        for key, value in pairs:
            if key.lower() not in _NOT_RETURNED_RESPONSE_HEADERS:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default request logging."""


class RecordingProxy:
    """Forward HTTP traffic to ``upstream`` and record each exchange to ``sink``.

    ``sink`` receives one :class:`Trace` per exchange, in the order the
    exchanges complete, before the response is written to the client. Ids are
    numbered ``<id_prefix>000001``, ``<id_prefix>000002``, and so on. An
    exchange whose upstream cannot be reached is answered with ``502`` and is
    not recorded.

    The socket is bound on construction, so :attr:`port` is known before
    :meth:`start`. Use the object as a context manager, or call :meth:`start`
    and :meth:`stop` yourself.
    """

    def __init__(
        self,
        upstream: str,
        sink: Callable[[Trace], None],
        host: str = "127.0.0.1",
        port: int = 0,
        clock: Callable[[], datetime] | None = None,
        id_prefix: str = "t-",
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_prefix = id_prefix
        self._host = host
        self._lock = threading.Lock()
        self._count = 0
        self.client = httpx.Client(timeout=30.0, follow_redirects=False)
        self._server = _ProxyServer((host, port), self)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The port the proxy listens on (the real one when constructed with port 0)."""
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """The base URL clients send their requests to."""
        return f"http://{self._host}:{self.port}"

    @property
    def count(self) -> int:
        """Number of exchanges recorded so far."""
        with self._lock:
            return self._count

    def record(self, request: Request, response: Response) -> None:
        """Number, timestamp and emit one exchange. Called from request threads."""
        with self._lock:
            self._count += 1
            self._sink(
                Trace(
                    id=f"{self._id_prefix}{self._count:06d}",
                    ts=self._clock().isoformat(),
                    request=request,
                    response=response,
                )
            )

    def start(self) -> "RecordingProxy":
        """Serve requests in a daemon thread. Calling it again has no effect. Returns self."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, name="recording-proxy", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving, release the socket and the HTTP client, and wait for the thread. Safe to call twice."""
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
            self._thread = None
        self._server.server_close()
        self.client.close()

    def __enter__(self) -> "RecordingProxy":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
