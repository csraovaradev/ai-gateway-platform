"""OpenTelemetry tracing (ADR-0004).

Spans are always created through the OTel API; they are no-ops unless `OTEL_EXPORTER_OTLP_ENDPOINT`
is set and the `otel` extra is installed, in which case they are exported over OTLP/HTTP. Standard
`OTEL_*` environment variables (service name, headers, sampler) apply.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import trace

tracer = trace.get_tracer("aigw")
_log = logging.getLogger(__name__)
_configured = False


def setup(app: Any) -> None:
    global _configured
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but aigw[otel] is not installed")
        return
    if not _configured:
        service = os.environ.get("OTEL_SERVICE_NAME", "aigw")
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _configured = True
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,metrics")


def current_trace_id() -> str | None:
    """Hex trace ID of the active span, for correlating audit lines with traces."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
