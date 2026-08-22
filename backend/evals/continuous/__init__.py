"""Fail-closed continuous evaluation runners for BreezeTravel.

Preflight remains independent from product code. Concrete import and Builder
adapters cross only the public HTTP API and emit immutable run evidence.
"""

from .core import PreflightResult, RunResult, preflight, run_foundation
from .http_builder import run_builder_http
from .http_import import HttpResponse, HttpTransportError, UrllibTransport, run_import_http

__all__ = [
    "HttpResponse",
    "HttpTransportError",
    "PreflightResult",
    "RunResult",
    "UrllibTransport",
    "preflight",
    "run_foundation",
    "run_builder_http",
    "run_import_http",
]
