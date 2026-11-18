"""The trace format: the one contract every stage of the pipeline shares.

A *trace* is one observed request/response exchange with the legacy system.
Traces are stored as JSON Lines (one trace per line) so recordings can be
streamed, appended to, diffed and grepped.

Shape of one line::

    {
      "id": "t-000042",
      "ts": "2026-11-18T19:30:00.123456+00:00",
      "request": {
        "method": "GET",
        "path": "/quote",
        "query_string": "sku=widget&qty=3",
        "params": {"sku": "widget", "qty": "3"},
        "headers": {"content-type": ""},
        "body": ""
      },
      "response": {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": "{\\"sku\\": \\"WIDGET\\", ...}"
      }
    }

``params`` is the analysis-friendly view of the request inputs: query string
parameters merged with form-encoded body fields (body wins on conflict, last
value wins for repeated keys). Values are always strings, exactly as sent.
The raw ``query_string`` and ``body`` are kept so a trace can be replayed
byte-for-byte.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qsl

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


@dataclass
class Request:
    method: str
    path: str
    query_string: str = ""
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @classmethod
    def build(
        cls,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
        headers: dict[str, str] | None = None,
    ) -> "Request":
        """Build a request, deriving ``params`` from the query string and form body."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        params = dict(parse_qsl(query_string, keep_blank_values=True))
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if body and ctype == FORM_CONTENT_TYPE:
            params.update(parse_qsl(body, keep_blank_values=True))
        return cls(
            method=method.upper(),
            path=path,
            query_string=query_string,
            params=params,
            headers=headers,
            body=body,
        )

    def key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        """Identity of a request for grouping: method, path and sorted params."""
        return (self.method, self.path, tuple(sorted(self.params.items())))


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json(self) -> Any | None:
        """The body parsed as JSON, or ``None`` if it is not JSON."""
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None


@dataclass
class Trace:
    id: str
    ts: str
    request: Request
    response: Response

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trace":
        return cls(
            id=data["id"],
            ts=data.get("ts", ""),
            request=Request(**data["request"]),
            response=Response(**data["response"]),
        )


def write_traces(path: str | Path, traces: Iterable[Trace], append: bool = False) -> int:
    """Write traces as JSON Lines. Returns the number written."""
    count = 0
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        for trace in traces:
            fh.write(json.dumps(trace.to_dict(), sort_keys=True) + "\n")
            count += 1
    return count


def iter_traces(path: str | Path) -> Iterator[Trace]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Trace.from_dict(json.loads(line))


def read_traces(path: str | Path) -> list[Trace]:
    return list(iter_traces(path))
