"""Optional, bounded and fail-open OpenTelemetry process runtime."""

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Tracer

from app.infrastructure.observability.settings import ObservabilitySettings
from app.infrastructure.observability.tracing import LangfuseMetadataSpanProcessor

SpanExporterFactory = Callable[[ObservabilitySettings], SpanExporter]
MetricExporterFactory = Callable[[ObservabilitySettings], Any]


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
            return
        try:
            await asyncio.wait_for(completed.wait(), timeout=self.settings.shutdown_timeout_seconds)
        except TimeoutError:
            self.shutdown_timed_out = True
        except Exception as error:
            self.shutdown_error_classes.append(type(error).__name__)

    def _shutdown_providers(self) -> None:
        # Stop metrics before traces so no final metric export outlives the trace provider.
        for provider in (self.meter_provider, self.tracer_provider):
            if provider is None:
                continue
            try:
                provider.shutdown()
            except Exception as error:
                self.shutdown_error_classes.append(type(error).__name__)


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
        return runtime

    span_factory = span_exporter_factory or _create_span_exporter
    metric_factory = metric_exporter_factory or _create_metric_exporter
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    try:
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": settings.service_version,
                "deployment.environment.name": settings.deployment_environment,
            }
        )
        span_exporter = span_factory(settings)
        tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
        )
        tracer_provider.add_span_processor(LangfuseMetadataSpanProcessor())
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                max_queue_size=settings.batch_max_queue_size,
                schedule_delay_millis=int(settings.batch_schedule_delay_seconds * 1000),
                max_export_batch_size=settings.batch_max_export_batch_size,
                export_timeout_millis=int(settings.export_timeout_seconds * 1000),
            )
        )

        metric_exporter = metric_factory(settings)
        metric_reader = PeriodicExportingMetricReader(
            metric_exporter,
            export_interval_millis=int(settings.metric_export_interval_seconds * 1000),
            export_timeout_millis=int(settings.export_timeout_seconds * 1000),
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    except Exception as error:
        runtime.initialization_error_class = type(error).__name__
        _shutdown_partial(meter_provider, tracer_provider)
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


def _shutdown_partial(*providers: object | None) -> None:
    for provider in providers:
        if provider is None:
            continue
        try:
            provider.shutdown()  # type: ignore[attr-defined]
        except Exception:
            pass
