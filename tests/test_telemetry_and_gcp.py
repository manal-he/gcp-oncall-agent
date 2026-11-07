from datetime import UTC, datetime

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from oncall_agent.agent.runner import Investigation, reject_all
from oncall_agent.mcp_server.backends.gcp import build_log_filter, build_metric_filter
from oncall_agent.telemetry import setup_tracing


async def test_spans_emitted(settings):
    exporter = InMemorySpanExporter()
    setup_tracing(settings, SimpleSpanProcessor(exporter))
    await Investigation("oom-killed").run(settings, reject_all)
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "investigation" in names and "agent.report" in names
    assert any(n.startswith("execute_tool query_logs") for n in names)
    assert any(n.startswith("chat ") for n in names)
    tool_span = next(s for s in spans if s.name.startswith("execute_tool"))
    assert tool_span.attributes["gen_ai.tool.name"]


def test_log_filter_is_structured():
    f = build_log_filter(
        resource_type="cloud_run_revision",
        service="checkout",
        min_severity="ERROR",
        contains="TypeError",
        lookback_minutes=30,
        now=datetime(2026, 10, 14, 14, 30, tzinfo=UTC),
    )
    assert 'timestamp >= "2026-10-14T14:00:00Z"' in f
    assert "severity >= ERROR" in f and 'resource.type = "cloud_run_revision"' in f
    assert '"TypeError"' in f


def test_metric_filter():
    f = build_metric_filter("run_request_count_5xx", "checkout")
    assert 'metric.type = "run.googleapis.com/request_count"' in f
    assert 'response_code_class = "5xx"' in f and 'service_name = "checkout"' in f
