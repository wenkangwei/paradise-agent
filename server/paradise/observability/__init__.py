"""Paradise observability — prod-only tracing, metrics, structured logging.

Phase 6 — Observability sidecar.

All three modules degrade gracefully when their deps are missing
(so dev mode imports without errors). OTLP export only active when
PARADISE_MODE=prod.
"""
from .tracing import setup_tracing, get_tracer
from .metrics import get_meter, record_ttft, record_tool_call, record_cost
from .logging_config import setup_structlog
from .atif_exporter import AtifExporter

__all__ = [
    "setup_tracing",
    "get_tracer",
    "get_meter",
    "record_ttft",
    "record_tool_call",
    "record_cost",
    "setup_structlog",
    "AtifExporter",
]
