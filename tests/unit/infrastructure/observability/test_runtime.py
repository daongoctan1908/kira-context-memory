import asyncio
import threading
import time
from collections.abc import Sequence

from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.infrastructure.observability.runtime import (
    ObservabilityRuntime,
    create_observability_runtime,
)
from app.infrastructure.observability.settings import ObservabilitySettings


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

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return self.result

    def shutdown(self) -> None:
        self.shutdown_calls += 1


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
        self.release.wait(timeout=1)
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


async def test_export_rejection_does_not_reach_business_code() -> None:
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


async def test_queue_saturation_drops_telemetry_without_reaching_business_code() -> None:
    spans = BlockingSpanExporter()
    runtime = create_observability_runtime(
        settings(batch_max_queue_size=1, batch_max_export_batch_size=1),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: RecordingMetricExporter(),
    )
    tracer = runtime.get_tracer("test")
    with tracer.start_as_current_span("operation"):
        pass
    assert await asyncio.to_thread(spans.started.wait, 0.5)

    for _ in range(100):
        with tracer.start_as_current_span("operation"):
            pass

    spans.release.set()
    await runtime.shutdown()
    assert runtime.shutdown_error_classes == []


class SlowProvider:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        time.sleep(0.1)


async def test_shutdown_deadline_is_bounded_and_idempotent() -> None:
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


def test_metric_factory_failure_closes_the_partially_created_trace_provider() -> None:
    spans = RecordingSpanExporter()

    runtime = create_observability_runtime(
        settings(),
        span_exporter_factory=lambda _: spans,
        metric_exporter_factory=lambda _: (_ for _ in ()).throw(RuntimeError("private")),
    )

    assert runtime.enabled is False
    assert runtime.initialization_error_class == "RuntimeError"
    assert spans.shutdown_calls == 1
