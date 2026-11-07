"""OpenTelemetry tracing for agent steps, LLM calls and MCP tool calls.

Span names and attributes follow the OpenTelemetry GenAI semantic conventions where they
exist (``gen_ai.operation.name``, ``gen_ai.request.model``, ``gen_ai.usage.*``,
``gen_ai.tool.name``). Exporters:

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` set + ``pip install .[otlp]`` -> OTLP/HTTP (e.g. an OTel
  Collector forwarding to Cloud Trace),
* ``ONCALL_OTEL_CONSOLE=true`` -> pretty spans on stderr,
* otherwise a no-op provider (zero overhead in tests).
"""

from __future__ import annotations

import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

from oncall_agent import __version__
from oncall_agent.config import Settings

_configured = False


def setup_tracing(settings: Settings, extra_processor: SpanProcessor | None = None) -> None:
    global _configured
    if _configured:
        if extra_processor is not None:
            provider = trace.get_tracer_provider()
            if isinstance(provider, TracerProvider):
                provider.add_span_processor(extra_processor)
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": "gcp-oncall-agent", "service.version": __version__})
    )
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except ImportError:  # pragma: no cover
            print("OTLP endpoint set but exporter not installed: pip install .[otlp]", file=sys.stderr)
    if settings.otel_console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    if extra_processor is not None:
        provider.add_span_processor(extra_processor)
    trace.set_tracer_provider(provider)
    _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("oncall_agent", __version__)
