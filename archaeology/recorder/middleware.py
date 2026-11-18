"""WSGI middleware that records every request/response exchange as a :class:`Trace`."""

from __future__ import annotations

import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ..trace import Request, Response, Trace


def _read_body(environ: dict[str, Any]) -> bytes:
    """Read the full request body, or ``b""`` when it is absent or empty."""
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return b""
    return stream.read(length)


class ListSink(list):
    """A sink that collects traces in memory."""

    def __call__(self, trace: Trace) -> None:
        self.append(trace)


class JsonlSink:
    """A sink that appends one JSON line per trace to a file."""

    def __init__(self, path: str | Path, append: bool = False) -> None:
        self.path = Path(path)
        self._fh = open(self.path, "a" if append else "w", encoding="utf-8")

    def __call__(self, trace: Trace) -> None:
        self._fh.write(json.dumps(trace.to_dict(), sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        """Close the underlying file."""
        self._fh.close()

    def __enter__(self) -> "JsonlSink":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class RecordingMiddleware:
    """Wrap a WSGI app and emit one :class:`Trace` per exchange to ``sink``."""

    def __init__(
        self,
        app: Callable[..., Any],
        sink: Callable[[Trace], None],
        clock: Callable[[], datetime] | None = None,
        id_prefix: str = "t-",
    ) -> None:
        self._app = app
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_prefix = id_prefix
        self._count = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        """Number of exchanges recorded so far."""
        with self._lock:
            return self._count

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        body = _read_body(environ)
        environ["wsgi.input"] = io.BytesIO(body)

        content_type = environ.get("CONTENT_TYPE", "")
        request = Request.build(
            method=environ.get("REQUEST_METHOD", "GET"),
            path=environ.get("PATH_INFO", ""),
            query_string=environ.get("QUERY_STRING", ""),
            body=body.decode("utf-8", errors="replace"),
            headers={"content-type": content_type} if content_type else {},
        )

        captured: dict[str, Any] = {}

        def recording_start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = {k.lower(): v for k, v in headers}
            if exc_info is not None:
                return start_response(status, headers, exc_info)
            return start_response(status, headers)

        result = self._app(environ, recording_start_response)
        try:
            response_body = b"".join(result)
        finally:
            if hasattr(result, "close"):
                result.close()

        if "status" not in captured:
            raise RuntimeError("WSGI app returned without calling start_response")

        response = Response(
            status=captured["status"],
            headers=captured["headers"],
            body=response_body.decode("utf-8", errors="replace"),
        )
        with self._lock:
            self._count += 1
            trace = Trace(
                id=f"{self._id_prefix}{self._count:06d}",
                ts=self._clock().isoformat(),
                request=request,
                response=response,
            )
            self._sink(trace)
        return [response_body]
