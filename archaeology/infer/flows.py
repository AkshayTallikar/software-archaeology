"""Identifier flows: response values the server generated and clients send back later.

A *source* is a string at a flattened JSON path of a response whose value no
earlier request carried, so the server must have generated it (or it is a
value the server first introduced). A *use* is a later request that carries
the same value as a whole path segment or as a whole parameter value. Uses are
counted per (source endpoint, source field, target endpoint, location) and
groups seen fewer than :data:`MIN_FLOW_COUNT` times are dropped.

Stateful recordings keep runs apart: a scenario trace only ever uses values
from its own run, so the same identifier in two runs is not a flow.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any

from ..trace import Trace
from .catalog import endpoint_key, flatten_json, json_object

MIN_SOURCE_LENGTH = 4
MIN_FLOW_COUNT = 2


@dataclass(frozen=True)
class _Source:
    """A generated value: where it was first returned and which run it belongs to."""

    index: int
    scope: Hashable
    endpoint: str
    field: str
    value: str


def _scope(trace: Trace) -> Hashable:
    """Values flow only within a scope: one scenario run, or all stateless traces."""
    if trace.scenario is None:
        return None
    return int(trace.scenario["run"])


def _tokens(trace: Trace) -> set[tuple[str, str]]:
    """``(value, location)`` for every path segment and parameter value of a request."""
    request = trace.request
    found = {(segment, "path") for segment in request.path.split("/") if segment}
    found.update((value, f"param {name}") for name, value in request.params.items())
    return found


def _response_strings(trace: Trace) -> list[tuple[str, str]]:
    """``(field path, value)`` for every long string field of a JSON object response."""
    obj = json_object(trace.response)
    if obj is None:
        return []
    return [
        (path, value)
        for path, value in flatten_json(obj).items()
        if isinstance(value, str) and len(value) >= MIN_SOURCE_LENGTH
    ]


def _find_sources(traces: Sequence[Trace], keys: Sequence[str]) -> list[_Source]:
    """Every response value that no request before it (or earlier response) carried.

    Each generated value is a source once: a later response repeating it is an
    echo of the same identifier, not a new one.
    """
    known: set[str] = set()
    sources: list[_Source] = []
    for index, trace in enumerate(traces):
        known.update(value for value, _ in _tokens(trace))
        for path, value in _response_strings(trace):
            if value not in known:
                known.add(value)
                sources.append(_Source(index, _scope(trace), keys[index], path, value))
    return sources


def _index_requests(
    traces: Sequence[Trace],
) -> dict[tuple[Hashable, str], list[tuple[int, str]]]:
    """Map ``(scope, value)`` to the ``(trace index, location)`` pairs that send it, in order."""
    index: dict[tuple[Hashable, str], list[tuple[int, str]]] = {}
    for position, trace in enumerate(traces):
        scope = _scope(trace)
        for value, location in _tokens(trace):
            if len(value) >= MIN_SOURCE_LENGTH:
                index.setdefault((scope, value), []).append((position, location))
    return index


def detect_flows(traces: Sequence[Trace]) -> list[dict[str, Any]]:
    """Find which response field feeds which later request, and where it is sent.

    Returns one dict per flow, sorted by (from, field, to, as)::

        {"from": "POST /members", "field": "member_id", "to": "POST /loans",
         "as": "param member_id", "count": 40,
         "description": "member_id from POST /members is sent to POST /loans as param member_id (40 times)"}
    """
    traces = list(traces)
    keys = [endpoint_key(trace) for trace in traces]
    index = _index_requests(traces)

    counts: Counter[tuple[str, str, str, str]] = Counter()
    for source in _find_sources(traces, keys):
        for position, location in index.get((source.scope, source.value), ()):
            if position > source.index:
                counts[(source.endpoint, source.field, keys[position], location)] += 1

    flows = [
        {
            "from": src,
            "field": field,
            "to": dst,
            "as": location,
            "count": count,
            "description": (
                f"{field} from {src} is sent to {dst} as {location} ({count} times)"
            ),
        }
        for (src, field, dst, location), count in counts.items()
        if count >= MIN_FLOW_COUNT
    ]
    flows.sort(key=lambda f: (f["from"], f["field"], f["to"], f["as"]))
    return flows
