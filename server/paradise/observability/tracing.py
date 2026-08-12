"""OpenTelemetry tracing setup.

Phase 6 — exports spans to OTel collector (see docker-compose.yml
otel-collector service). When OTEL_EXPORTER_OTLP_ENDPOINT unset, tracing
is no-op (spans are recorded but not exported).

Usage:
    setup_tracing(endpoint="http://otel-collector:4317", service_name="paradise-api")
    tracer = get_tracer()
    with tracer.start_as_current_span("handle_message"):
        ...
"""
from __future__ import annotations

import logging

logger = logging.getLogger("paradise.observability.tracing")

_tracer = None  # set by setup_tracing

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        _HAS_OTEL = True
    except ImportError:
        OTLPSpanExporter = None  # type: ignore
        _HAS_OTEL = False
except ImportError:
    trace = None  # type: ignore
    _HAS_OTEL = False


def setup_tracing(
    endpoint: str | None = None,
    service_name: str = "paradise-api",
    service_version: str = "0.1.0",
) -> bool:
    """Initialise global TracerProvider with OTLP exporter.

    Returns True if tracing is active, False if no-op (deps missing).
    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _tracer
    if _tracer is not None:
        return True
    if not _HAS_OTEL or trace is None:
        logger.info("OTel tracing: deps missing — no-op mode")
        _tracer = _NoopTracer()
        return False

    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
    })
    provider = TracerProvider(resource=resource)

    if endpoint:
        try:
            exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("OTel tracing: exporting to %s", endpoint)
        except Exception as exc:
            logger.warning("OTel exporter setup failed: %s — falling back to no-op", exc)

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("paradise")
    return True


def get_tracer():
    """Return the configured tracer (or a no-op if setup_tracing not called)."""
    global _tracer
    if _tracer is None:
        _tracer = _NoopTracer()
    return _tracer


class _NoopTracer:
    """Drop-in for opentelemetry Tracer when deps missing.
    Implements start_as_current_span as a context manager that does nothing.
    """
    def start_as_current_span(self, name, **kwargs):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield _NoopSpan()

        return _cm()


class _NoopSpan:
    def set_attribute(self, key, value):
        pass

    def record_exception(self, exc):
        pass

    def add_event(self, name, **kwargs):
        pass
