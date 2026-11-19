"""Adapters that turn a system under test into a ``Request -> Response`` callable.

* :class:`WsgiTarget` runs an in-process WSGI application.
* :class:`AsgiTarget` runs an in-process ASGI application (FastAPI, Starlette).
* :class:`HttpTarget` sends each request to a live base URL.
* :class:`ProcessTarget` starts a system as a child process on a free port and
  sends each request to it over HTTP, so systems in any language can be
  excavated and replayed.

:func:`load_target` picks the adapter from a spec string.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import shlex
import socket
import subprocess
import time
from typing import Any, Callable

import httpx

from archaeology.trace import Request, Response
from archaeology.wsgi import WSGIApp, call_wsgi

Target = Callable[[Request], Response]

ASGI_BASE_URL = "http://testserver"


def _target_url(request: Request) -> str:
    """Path plus the raw query string, exactly as recorded (no re-encoding)."""
    if request.query_string:
        return f"{request.path}?{request.query_string}"
    return request.path


def _request_headers(request: Request) -> dict[str, str] | None:
    content_type = request.headers.get("content-type")
    return {"content-type": content_type} if content_type else None


def _request_content(request: Request) -> bytes | None:
    return request.body.encode("utf-8") if request.body else None


def _to_response(raw: httpx.Response) -> Response:
    headers = {k.lower(): v for k, v in raw.headers.multi_items()}
    return Response(
        status=raw.status_code,
        headers=headers,
        body=raw.content.decode("utf-8", errors="replace"),
    )


class WsgiTarget:
    """Run each request through an in-process WSGI application."""

    def __init__(self, app: WSGIApp) -> None:
        self.app = app

    def __call__(self, request: Request) -> Response:
        return call_wsgi(self.app, request)


class AsgiTarget:
    """Run each request through an in-process ASGI application.

    One private event loop and one ``httpx.AsyncClient`` are kept for the
    target's lifetime. Call :meth:`close` (or use it as a context manager)
    to release them.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._loop = asyncio.new_event_loop()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ASGI_BASE_URL,
        )

    async def _send(self, request: Request) -> httpx.Response:
        return await self._client.request(
            request.method,
            _target_url(request),
            content=_request_content(request),
            headers=_request_headers(request),
        )

    def __call__(self, request: Request) -> Response:
        if self._loop.is_closed():
            raise RuntimeError("AsgiTarget is closed")
        raw = self._loop.run_until_complete(self._send(request))
        return _to_response(raw)

    def close(self) -> None:
        """Close the HTTP client and the private event loop. Safe to call twice."""
        if self._loop.is_closed():
            return
        try:
            self._loop.run_until_complete(self._client.aclose())
        finally:
            self._loop.close()

    def __enter__(self) -> "AsgiTarget":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class HttpTarget:
    """Send each request to a live server at ``base_url``."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def __call__(self, request: Request) -> Response:
        raw = self._client.request(
            request.method,
            _target_url(request),
            content=_request_content(request),
            headers=_request_headers(request),
        )
        return _to_response(raw)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpTarget":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def free_port(host: str = "127.0.0.1") -> int:
    """A TCP port that was free a moment ago."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: float, process: subprocess.Popen | None = None) -> None:
    """Block until ``host:port`` accepts connections; fail fast if ``process`` exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"process exited with code {process.returncode} before listening on {port}")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"nothing listening on {host}:{port} after {timeout}s")


class ProcessTarget:
    """Start ``command`` as a child process and talk to it over HTTP.

    The command must listen on the port given in the ``PORT`` environment
    variable (``HOST`` is set too). Every instance is a fresh process, so it
    starts from empty state. Call :meth:`close` to stop it.
    """

    def __init__(self, command: str | list[str], timeout: float = 10.0, cwd: str | None = None) -> None:
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        self.host = "127.0.0.1"
        self.port = free_port(self.host)
        env = dict(os.environ, PORT=str(self.port), HOST=self.host)
        self._process = subprocess.Popen(
            self.command,
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_port(self.host, self.port, timeout, self._process)
        except Exception as exc:
            stderr = self._stop()
            raise RuntimeError(f"could not start {self.command!r}: {exc}\n{stderr}") from exc
        self.base_url = f"http://{self.host}:{self.port}"
        self._http = HttpTarget(self.base_url, timeout=timeout)

    def __call__(self, request: Request) -> Response:
        return self._http(request)

    def _stop(self) -> str:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        stderr = self._process.stderr.read().decode("utf-8", "replace") if self._process.stderr else ""
        if self._process.stderr:
            self._process.stderr.close()
        return stderr

    def close(self) -> None:
        """Stop the HTTP client and the process. Safe to call twice."""
        http = getattr(self, "_http", None)
        if http is not None:
            http.close()
            self._http = None
        if self._process.returncode is None:
            self._stop()

    def __enter__(self) -> "ProcessTarget":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _is_asgi_app(obj: Any) -> bool:
    """True for FastAPI/Starlette apps and other objects with an async ``__call__``."""
    if inspect.iscoroutinefunction(obj):
        return True
    return inspect.iscoroutinefunction(getattr(obj, "__call__", None))


def load_target(spec: str) -> Target:
    """Build a target from ``"module.path:attr"``, ``"cmd:<command>"`` or an ``http(s)://`` base URL.

    A ``make_*`` factory function is called first, for fresh state. An ASGI
    app (or any object whose ``__call__`` is a coroutine function)
    becomes an :class:`AsgiTarget`; any other callable becomes a
    :class:`WsgiTarget`.
    """
    if spec.startswith(("http://", "https://")):
        return HttpTarget(spec)
    if spec.startswith("cmd:"):
        return ProcessTarget(spec[len("cmd:"):].strip())
    module_name, sep, attr_path = spec.partition(":")
    if not sep or not module_name or not attr_path:
        raise ValueError(
            f"target spec must be 'module:attr' or an http(s) URL, got {spec!r}"
        )
    obj: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    # A ``make_*`` factory is called, so every replay starts from fresh state.
    if inspect.isfunction(obj) and obj.__name__.startswith("make_"):
        obj = obj()
    if _is_asgi_app(obj):
        return AsgiTarget(obj)
    if callable(obj):
        return WsgiTarget(obj)
    raise TypeError(f"{spec!r} is not callable")
