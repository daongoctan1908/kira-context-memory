"""Optional, bounded and fail-open OpenTelemetry process runtime."""

import asyncio
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Tracer

from app.infrastructure.observability.logging import report_telemetry_diagnostic
from app.infrastructure.observability.settings import ObservabilitySettings
from app.infrastructure.observability.tracing import LangfuseMetadataSpanProcessor

SpanExporterFactory = Callable[[ObservabilitySettings], SpanExporter]
MetricExporterFactory = Callable[[ObservabilitySettings], Any]


class _DiagnosticSpanExporter(SpanExporter):
    """Observe exporter return values, which the SDK otherwise silently ignores."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._exporter.export(spans)
        except Exception as error:
            report_telemetry_diagnostic(
                "telemetry.export_failed", signal="traces", error_class=type(error).__name__
            )
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            report_telemetry_diagnostic(
                "telemetry.export_failed", signal="traces", error_class="ExportFailure"
            )
        return result

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._exporter.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        self._exporter.shutdown()


class _DiagnosticMetricExporter(MetricExporter):
    """Preserve metric exporter preferences while exposing export failures locally."""

    def __init__(self, exporter: MetricExporter) -> None:
        super().__init__(
            preferred_temporality=exporter._preferred_temporality,
            preferred_aggregation=exporter._preferred_aggregation,
        )
        self._exporter = exporter

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        try:
            result = self._exporter.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
        except Exception as error:
            report_telemetry_diagnostic(
                "telemetry.export_failed", signal="metrics", error_class=type(error).__name__
            )
            return MetricExportResult.FAILURE
        if result is not MetricExportResult.SUCCESS:
            report_telemetry_diagnostic(
                "telemetry.export_failed", signal="metrics", error_class="ExportFailure"
            )
        return result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self._exporter.force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        self._exporter.shutdown(timeout_millis=timeout_millis, **kwargs)


@dataclass(slots=True)
class ObservabilityRuntime:
    """Own the providers for one Gateway or Worker process lifespan."""

    settings: ObservabilitySettings
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    initialization_error_class: str | None = None
    shutdown_error_classes: list[str] = field(default_factory=list)
    shutdown_timed_out: bool = False
    _shutdown_started: bool = False

    @property
    def enabled(self) -> bool:
        return self.tracer_provider is not None and self.meter_provider is not None

    def get_tracer(self, name: str, version: str | None = None) -> Tracer:
        provider = self.tracer_provider or trace.NoOpTracerProvider()
        try:
            return provider.get_tracer(name, version)
        except Exception:
            return trace.NoOpTracerProvider().get_tracer(name, version)

    def get_meter(self, name: str, version: str | None = None) -> Meter:
        provider = self.meter_provider or metrics.NoOpMeterProvider()
        try:
            return provider.get_meter(name, version)
        except Exception:
            return metrics.NoOpMeterProvider().get_meter(name, version)

    async def shutdown(self) -> None:
        """Shut providers down once, bounded by the configured application deadline."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        if self.tracer_provider is None and self.meter_provider is None:
            return
        loop = asyncio.get_running_loop()
        completed = asyncio.Event()

        def shutdown_in_background() -> None:
            try:
                self._shutdown_providers()
            finally:
                try:
                    loop.call_soon_threadsafe(completed.set)
                except RuntimeError:
                    pass

        # A dedicated daemon avoids making event-loop executor shutdown wait on a stuck exporter.
        thread = threading.Thread(
            target=shutdown_in_background,
            name="otel-provider-shutdown",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as error:
            self.shutdown_error_classes.append(type(error).__name__)
            report_telemetry_diagnostic(
                "telemetry.shutdown_failed", error_class=type(error).__name__
            )
            return
        try:
            await asyncio.wait_for(completed.wait(), timeout=self.settings.shutdown_timeout_seconds)
        except TimeoutError:
            self.shutdown_timed_out = True
            report_telemetry_diagnostic("telemetry.shutdown_timeout", error_class="ShutdownTimeout")
        except Exception as error:
            self.shutdown_error_classes.append(type(error).__name__)
            report_telemetry_diagnostic(
                "telemetry.shutdown_failed", error_class=type(error).__name__
            )

    def _shutdown_providers(self, providers: Sequence[object | None] | None = None) -> None:
        # Stop metrics before traces so no final metric export outlives the trace provider.
        resolved_providers = (
            providers if providers is not None else (self.meter_provider, self.tracer_provider)
        )
        for provider in resolved_providers:
            if provider is None:
                continue
            try:
                provider.shutdown()  # type: ignore[attr-defined]
            except Exception as error:
                self.shutdown_error_classes.append(type(error).__name__)
                report_telemetry_diagnostic(
                    "telemetry.shutdown_failed", error_class=type(error).__name__
                )


def create_observability_runtime(
    settings: ObservabilitySettings,
    *,
    span_exporter_factory: SpanExporterFactory | None = None,
    metric_exporter_factory: MetricExporterFactory | None = None,
) -> ObservabilityRuntime:
    """Build both providers atomically or return a no-op runtime on recoverable failures."""
    runtime = ObservabilityRuntime(settings=settings)
    if not settings.enabled:
        return runtime
    if settings.otlp_endpoint is None:
        runtime.initialization_error_class = "MissingOtlpEndpointError"
        report_telemetry_diagnostic(
            "telemetry.initialization_failed", error_class="MissingOtlpEndpointError"
        )
        return runtime

    span_factory = span_exporter_factory or _create_span_exporter
    metric_factory = metric_exporter_factory or _create_metric_exporter
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    span_exporter: SpanExporter | None = None
    metric_exporter: MetricExporter | None = None
    metric_reader: PeriodicExportingMetricReader | None = None
    try:
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": settings.service_version,
                "deployment.environment.name": settings.deployment_environment,
            }
        )
        span_exporter = _DiagnosticSpanExporter(span_factory(settings))
        tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
            shutdown_on_exit=False,
        )
        tracer_provider.add_span_processor(
            LangfuseMetadataSpanProcessor(capture_content=settings.capture_content_enabled)
        )
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                max_queue_size=settings.batch_max_queue_size,
                schedule_delay_millis=int(settings.batch_schedule_delay_seconds * 1000),
                max_export_batch_size=settings.batch_max_export_batch_size,
                export_timeout_millis=int(settings.export_timeout_seconds * 1000),
            )
        )

        metric_exporter = _DiagnosticMetricExporter(metric_factory(settings))
        metric_reader = PeriodicExportingMetricReader(
            metric_exporter,
            export_interval_millis=int(settings.metric_export_interval_seconds * 1000),
            export_timeout_millis=int(settings.export_timeout_seconds * 1000),
        )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader], shutdown_on_exit=False
        )
    except Exception as error:
        runtime.initialization_error_class = type(error).__name__
        report_telemetry_diagnostic(
            "telemetry.initialization_failed", error_class=type(error).__name__
        )
        _shutdown_partial(
            runtime,
            meter_provider or metric_reader or metric_exporter,
            tracer_provider or span_exporter,
        )
        return runtime

    runtime.tracer_provider = tracer_provider
    runtime.meter_provider = meter_provider
    return runtime


def _create_span_exporter(settings: ObservabilitySettings) -> SpanExporter:
    endpoint = settings.signal_endpoint("traces")
    if endpoint is None:  # guarded before factory invocation
        raise ValueError("OTLP traces endpoint is required")
    return OTLPSpanExporter(endpoint=endpoint, timeout=settings.export_timeout_seconds)


def _create_metric_exporter(settings: ObservabilitySettings) -> OTLPMetricExporter:
    endpoint = settings.signal_endpoint("metrics")
    if endpoint is None:  # guarded before factory invocation
        raise ValueError("OTLP metrics endpoint is required")
    return OTLPMetricExporter(endpoint=endpoint, timeout=settings.export_timeout_seconds)


def _shutdown_partial(runtime: ObservabilityRuntime, *providers: object | None) -> None:
    """Clean failed startup in daemon threads without blocking application availability."""
    for provider in providers:
        if provider is None:
            continue
        completed = threading.Event()

        def cleanup(
            partial_provider: object = provider,
            partial_completed: threading.Event = completed,
        ) -> None:
            try:
                runtime._shutdown_providers((partial_provider,))
            finally:
                partial_completed.set()

        def monitor(partial_completed: threading.Event = completed) -> None:
            if not partial_completed.wait(timeout=runtime.settings.shutdown_timeout_seconds):
                runtime.shutdown_timed_out = True
                report_telemetry_diagnostic(
                    "telemetry.shutdown_timeout", error_class="ShutdownTimeout"
                )

        try:
            threading.Thread(target=cleanup, name="otel-startup-cleanup", daemon=True).start()
            threading.Thread(
                target=monitor, name="otel-startup-cleanup-monitor", daemon=True
            ).start()
        except Exception as error:
            runtime.shutdown_error_classes.append(type(error).__name__)
            report_telemetry_diagnostic(
                "telemetry.shutdown_failed", error_class=type(error).__name__
            )
