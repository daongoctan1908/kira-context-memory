"""Validated, process-neutral settings for the observability runtime."""

from dataclasses import dataclass
from typing import Protocol


class ObservabilitySettingsSource(Protocol):
    """Settings fields shared by the Gateway and Worker models."""

    app_environment: str
    app_version: str
    otel_enabled: bool
    otel_exporter_otlp_endpoint: object | None
    otel_export_timeout_seconds: float
    otel_batch_schedule_delay_seconds: float
    otel_batch_max_queue_size: int
    otel_batch_max_export_batch_size: int
    otel_metric_export_interval_seconds: float
    otel_trace_sample_ratio: float
    otel_shutdown_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    """Small immutable contract consumed by the shared OTel runtime."""

    enabled: bool
    service_name: str
    service_version: str
    deployment_environment: str
    otlp_endpoint: str | None
    export_timeout_seconds: float
    batch_schedule_delay_seconds: float
    batch_max_queue_size: int
    batch_max_export_batch_size: int
    metric_export_interval_seconds: float
    trace_sample_ratio: float
    shutdown_timeout_seconds: float

    def signal_endpoint(self, signal: str) -> str | None:
        """Return an OTLP/HTTP signal URL without accepting arbitrary paths."""
        if self.otlp_endpoint is None:
            return None
        if signal not in {"traces", "metrics"}:
            raise ValueError("unsupported OTLP signal")
        return f"{self.otlp_endpoint.rstrip('/')}/v1/{signal}"


def build_observability_settings(
    source: ObservabilitySettingsSource,
    *,
    service_name: str,
) -> ObservabilitySettings:
    """Copy only reviewed settings from an application model into the runtime contract."""
    endpoint = source.otel_exporter_otlp_endpoint
    return ObservabilitySettings(
        enabled=source.otel_enabled,
        service_name=service_name,
        service_version=source.app_version,
        deployment_environment=source.app_environment,
        otlp_endpoint=str(endpoint).rstrip("/") if endpoint is not None else None,
        export_timeout_seconds=source.otel_export_timeout_seconds,
        batch_schedule_delay_seconds=source.otel_batch_schedule_delay_seconds,
        batch_max_queue_size=source.otel_batch_max_queue_size,
        batch_max_export_batch_size=source.otel_batch_max_export_batch_size,
        metric_export_interval_seconds=source.otel_metric_export_interval_seconds,
        trace_sample_ratio=source.otel_trace_sample_ratio,
        shutdown_timeout_seconds=source.otel_shutdown_timeout_seconds,
    )
