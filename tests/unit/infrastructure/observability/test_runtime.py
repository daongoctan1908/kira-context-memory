import asyncio
import io
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator, Sequence

import pytest
from opentelemetry.sdk._shared_internal import DuplicateFilter
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.infrastructure.observability import runtime as runtime_module
from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.logging import (
    SafeJsonFormatter,
    SafeTelemetryDiagnosticHandler,
)
from app.infrastructure.observability.runtime import (
    ObservabilityRuntime,
    create_observability_runtime,
)
from app.infrastructure.observability.settings import ObservabilitySettings


@pytest.fixture
def diagnostic_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[io.StringIO, SafeTelemetryDiagnosticHandler]]:
    stream = io.StringIO()
    handler = SafeTelemetryDiagnosticHandler(stream)
    handler.setFormatter(SafeJsonFormatter(service_name="test-service"))
    logger = logging.getLogger("opentelemetry")
    original_handlers, original_level, original_propagate = (
        logger.handlers[:],
        logger.level,
        logger.propagate,
    )
    logger.handlers = [handler]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    # In-process Alembic migrations use fileConfig, which disables previously imported loggers.
    # Restore an isolated SDK path so these tests exercise real emitted diagnostics in any order.
    sdk_logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    original_sdk_level = sdk_logger.level
    monkeypatch.setattr(sdk_logger, "disabled", False)
    monkeypatch.setattr(sdk_logger, "propagate", True)
    sdk_logger.setLevel(logging.NOTSET)
    try:
        yield stream, handler
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        sdk_logger.setLevel(original_sdk_level)


def settings(**overrides: object) -> ObservabilitySettings:
    values: dict[str, object] = {
        "enabled": True,
        "service_name": "test-service",
        "service_version": "0.4.1",
        "deployment_environment": "test",
        "otlp_endpoint": "http://collector.test:4318",
        "export_timeout_seconds": 0.05,
        "batch_schedule_delay_seconds": 0.01,
        "batch_max_queue_size": 16,
        "batch_max_export_batch_size": 4,
        "metric_export_interval_seconds": 60.0,
        "trace_sample_ratio": 1.0,
        "shutdown_timeout_seconds": 0.5,
    }
    values.update(overrides)
    return ObservabilitySettings(**values)  # type: ignore[arg-type]


class RecordingSpanExporter(SpanExporter):
    def __init__(self, *, result: SpanExportResult = SpanExportResult.SUCCESS) -> None:
        self.result = result
        self.spans: list[ReadableSpan] = []
        self.shutdown_calls = 0
        self.shutdown_finished = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return self.result

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.shutdown_finished.set()


class RecordingMetricExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.exports: list[MetricsData] = []
        self.shutdown_calls = 0

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        self.exports.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        self.shutdown_calls += 1


class BlockingSpanExporter(RecordingSpanExporter):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.started.set()
        # Only the test releases export, so coverage or machine load cannot drain the queue early.
        self.release.wait()
        return super().export(spans)


async def test_enabled_runtime_exports_and_shuts_down_exactly_once() -> None:
    spans = RecordingSpanExporter()
    metrics = RecordingMetricExporter()
    runtime = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: metrics,
    )

    assert runtime.enabled is True
    tracer = runtime.get_tracer("test")
    meter = runtime.get_meter("test")
    with tracer.start_as_current_span("operation"):
        meter.create_counter("test.counter").add(1)

    await runtime.shutdown()
    await runtime.shutdown()

    assert len(spans.spans) == 1
    assert spans.spans[0].resource.attributes["service.name"] == "test-service"
    assert spans.shutdown_calls == 1
    assert metrics.shutdown_calls == 1


def test_disabled_or_failed_initialization_returns_noop_runtime() -> None:
    calls = 0

    def forbidden_factory(_: ObservabilitySettings) -> SpanExporter:
        nonlocal calls
        calls += 1
        raise AssertionError

    disabled = create_observability_runtime(
        settings(enabled=False),
        span_exporter_factory=forbidden_factory,
    )
    missing = create_observability_runtime(settings(otlp_endpoint=None))
    failed = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: (_ for _ in ()).throw(RuntimeError("private")),
    )

    assert disabled.enabled is False
    assert missing.initialization_error_class == "MissingOtlpEndpointError"
    assert failed.initialization_error_class == "RuntimeError"
    assert calls == 0


async def test_export_rejection_does_not_reach_business_code(
    diagnostic_sink: tuple[io.StringIO, SafeTelemetryDiagnosticHandler],
) -> None:
    spans = RecordingSpanExporter(result=SpanExportResult.FAILURE)
    runtime = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: RecordingMetricExporter(),
    )
    tracer = runtime.get_tracer("test")

    with tracer.start_as_current_span("operation"):
        pass

    await runtime.shutdown()
    assert runtime.shutdown_error_classes == []
    payload = json.loads(diagnostic_sink[0].getvalue())
    assert payload["event"] == "telemetry.export_failed"
    assert payload["operation"] == "traces"


async def test_queue_saturation_drops_telemetry_without_reaching_business_code(
    diagnostic_sink: tuple[io.StringIO, SafeTelemetryDiagnosticHandler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SDK duplicate suppression is process-global; earlier saturation tests may use the same window.
    for sdk_filter in logging.getLogger("opentelemetry.sdk._shared_internal").filters:
        if isinstance(sdk_filter, DuplicateFilter):
            monkeypatch.setattr(sdk_filter, "last_log", None, raising=False)
    spans = BlockingSpanExporter()
    runtime = create_observability_runtime(
        settings(
            batch_max_queue_size=1,
            batch_max_export_batch_size=1,
            shutdown_timeout_seconds=5.0,
        ),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: RecordingMetricExporter(),
    )
    tracer = runtime.get_tracer("test")
    try:
        with tracer.start_as_current_span("operation"):
            pass
        assert await asyncio.to_thread(spans.started.wait, 5.0)

        for _ in range(100):
            with tracer.start_as_current_span("operation"):
                pass

        assert diagnostic_sink[1].diagnostic_counts[("telemetry.queue_dropped", "traces")] >= 1
    finally:
        # Unblock on assertion failure; runtime.shutdown separately bounds cleanup.
        spans.release.set()
        await runtime.shutdown()
    assert runtime.shutdown_error_classes == []
    assert len(spans.spans) == 2
    payload = json.loads(diagnostic_sink[0].getvalue())
    assert payload["event"] == "telemetry.queue_dropped"
    assert diagnostic_sink[1].diagnostic_counts[("telemetry.queue_dropped", "traces")] > 0


class SlowProvider:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        time.sleep(0.1)


async def test_shutdown_deadline_is_bounded_and_idempotent(
    diagnostic_sink: tuple[io.StringIO, SafeTelemetryDiagnosticHandler],
) -> None:
    provider = SlowProvider()
    runtime = ObservabilityRuntime(
        settings=settings(shutdown_timeout_seconds=0.01),
        tracer_provider=provider,  # type: ignore[arg-type]
    )

    started = asyncio.get_running_loop().time()
    await runtime.shutdown()
    elapsed = asyncio.get_running_loop().time() - started
    await runtime.shutdown()

    assert elapsed < 0.08
    assert runtime.shutdown_timed_out is True
    assert provider.shutdown_calls == 1
    assert json.loads(diagnostic_sink[0].getvalue())["event"] == "telemetry.shutdown_timeout"


def test_metric_factory_failure_closes_the_partially_created_trace_provider() -> None:
    spans = RecordingSpanExporter()

    runtime = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: (_ for _ in ()).throw(RuntimeError("private")),
    )

    assert runtime.enabled is False
    assert runtime.initialization_error_class == "RuntimeError"
    assert spans.shutdown_finished.wait(timeout=0.5)
    assert spans.shutdown_calls == 1


async def test_real_otlp_connection_refusal_is_observable_without_endpoint_or_secret(
    diagnostic_sink: tuple[io.StringIO, SafeTelemetryDiagnosticHandler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    # A bound socket with no listener refuses TCP connections while keeping the port reserved.
    with socket.socket() as refused_socket:
        refused_socket.bind(("127.0.0.1", 0))
        port = refused_socket.getsockname()[1]
        runtime = create_observability_runtime(
            settings(otlp_endpoint=f"http://127.0.0.1:{port}/private-provider?token=secret"),
            metric_exporter_factory=lambda _: RecordingMetricExporter(),
        )
        with runtime.get_tracer("test").start_as_current_span("operation"):
            pass
        await runtime.shutdown()

    payloads = [json.loads(line) for line in diagnostic_sink[0].getvalue().splitlines()]
    assert any(payload["event"] == "telemetry.export_failed" for payload in payloads)
    assert all(payload["operation"] == "traces" for payload in payloads)
    assert "127.0.0.1" not in diagnostic_sink[0].getvalue()
    assert "private-provider" not in diagnostic_sink[0].getvalue()
    assert "secret" not in diagnostic_sink[0].getvalue()
    assert "traceback" not in diagnostic_sink[0].getvalue()


async def test_metric_export_failure_is_observable_and_fail_open(
    diagnostic_sink: tuple[io.StringIO, SafeTelemetryDiagnosticHandler],
) -> None:
    class FailingMetricExporter(RecordingMetricExporter):
        def export(
            self,
            metrics_data: MetricsData,
            timeout_millis: float = 10_000,
            **kwargs: object,
        ) -> MetricExportResult:
            return MetricExportResult.FAILURE

    runtime = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: RecordingSpanExporter(),
        metric_exporter_factory=lambda _: FailingMetricExporter(),
    )
    runtime.get_meter("test").create_counter("test.counter").add(1)
    await runtime.shutdown()

    payload = json.loads(diagnostic_sink[0].getvalue())
    assert payload["event"] == "telemetry.export_failed"
    assert payload["operation"] == "metrics"
    assert runtime.shutdown_error_classes == []


async def test_export_exception_and_provider_shutdown_errors_do_not_expose_message(
    diagnostic_sink: tuple[io.StringIO, SafeTelemetryDiagnosticHandler],
) -> None:
    class ThrowingSpanExporter(RecordingSpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            raise RuntimeError("private http://provider?token=secret")

        def shutdown(self) -> None:
            raise ValueError("private http://provider?token=secret")

    runtime = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: ThrowingSpanExporter(),
        metric_exporter_factory=lambda _: RecordingMetricExporter(),
    )
    with runtime.get_tracer("test").start_as_current_span("operation"):
        pass
    await runtime.shutdown()

    payloads = [json.loads(line) for line in diagnostic_sink[0].getvalue().splitlines()]
    assert {payload["event"] for payload in payloads} == {
        "telemetry.export_failed",
        "telemetry.shutdown_failed",
    }
    assert runtime.shutdown_error_classes == ["ValueError"]
    assert "private" not in diagnostic_sink[0].getvalue()
    assert "secret" not in diagnostic_sink[0].getvalue()
    assert "provider" not in diagnostic_sink[0].getvalue()


async def test_failed_startup_cleanup_never_blocks_on_a_stuck_exporter(
    diagnostic_sink: tuple[io.StringIO, SafeTelemetryDiagnosticHandler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StuckShutdownExporter(RecordingSpanExporter):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def shutdown(self) -> None:
            self.started.set()
            self.release.wait(timeout=1)
            super().shutdown()

    spans = StuckShutdownExporter()
    timeout_reported = threading.Event()
    original_report = runtime_module.report_telemetry_diagnostic

    def capture_timeout(event: str, **kwargs: str) -> None:
        original_report(event, **kwargs)
        if event == "telemetry.shutdown_timeout":
            timeout_reported.set()

    monkeypatch.setattr(runtime_module, "report_telemetry_diagnostic", capture_timeout)
    started = time.monotonic()
    runtime = create_observability_runtime(
        settings(shutdown_timeout_seconds=0.01),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 0.08
        assert runtime.enabled is False
        assert await asyncio.to_thread(spans.started.wait, 0.5)
        assert await asyncio.to_thread(timeout_reported.wait, 0.5)
        assert runtime.shutdown_timed_out is True
        payloads = [json.loads(line) for line in diagnostic_sink[0].getvalue().splitlines()]
        assert {payload["event"] for payload in payloads} == {
            "telemetry.initialization_failed",
            "telemetry.shutdown_timeout",
        }
        assert "secret" not in diagnostic_sink[0].getvalue()
    finally:
        spans.release.set()
        assert await asyncio.to_thread(spans.shutdown_finished.wait, 0.5)


async def test_runtime_copies_request_local_ids_to_direct_child_spans() -> None:
    spans = RecordingSpanExporter()
    runtime = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: RecordingMetricExporter(),
    )
    tracer = runtime.get_tracer("test")

    with bind_observability_context(
        correlation_id="correlation-1",
        turn_id="turn-1",
        event_id="event-1",
        origin_trace_id="origin-1",
    ):
        with tracer.start_as_current_span("direct-span"):
            pass

    await runtime.shutdown()
    attributes = spans.spans[0].attributes
    for key, value in {
        "correlation_id": "correlation-1",
        "turn_id": "turn-1",
        "event_id": "event-1",
        "origin_trace_id": "origin-1",
    }.items():
        assert attributes[f"langfuse.trace.metadata.{key}"] == value
