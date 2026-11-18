"""Inference engine: infer a system's true behaviour from recorded traces."""

from .boundaries import detect_boundaries
from .catalog import build_catalog, endpoint_key, json_type, path_template, value_type
from .claims import check_claims, load_claims
from .normalization import detect_normalization
from .spec import infer_spec, load_spec, render_report, save_spec, volatile_paths_for
from .volatility import detect_volatile_fields, flatten_json, volatile_paths

__all__ = [
    "build_catalog",
    "check_claims",
    "detect_boundaries",
    "detect_normalization",
    "detect_volatile_fields",
    "endpoint_key",
    "flatten_json",
    "infer_spec",
    "json_type",
    "load_claims",
    "load_spec",
    "path_template",
    "render_report",
    "save_spec",
    "value_type",
    "volatile_paths",
    "volatile_paths_for",
]
