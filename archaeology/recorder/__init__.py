"""Recorder stage: capture real request/response traffic as traces."""

from .exerciser import (
    expand_sweep,
    expand_workload,
    exercise,
    exercise_target,
    load_workload,
    record,
    record_target,
    request_from_spec,
)
from .middleware import JsonlSink, ListSink, RecordingMiddleware
from .proxy import RecordingProxy

__all__ = [
    "JsonlSink",
    "ListSink",
    "RecordingMiddleware",
    "RecordingProxy",
    "expand_sweep",
    "expand_workload",
    "exercise",
    "exercise_target",
    "load_workload",
    "record",
    "record_target",
    "request_from_spec",
]
