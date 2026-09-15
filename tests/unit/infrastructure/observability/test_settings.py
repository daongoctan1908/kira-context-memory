from types import SimpleNamespace

import pytest

from app.infrastructure.observability.settings import build_observability_settings


def source(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "app_environment": "test",
        "app_version": "0.4.1",
        "otel_enabled": True,
        "otel_exporter_otlp_endpoint": "http://collector:4318/",
        "otel_export_timeout_seconds": 1.0,
        "otel_batch_schedule_delay_seconds": 5.0,
        "otel_batch_max_queue_size": 2048,
        "otel_batch_max_export_batch_size": 512,
        "otel_metric_export_interval_seconds": 15.0,
        "otel_trace_sample_ratio": 1.0,
        "otel_shutdown_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_settings_copies_only_shared_contract_and_builds_signal_urls() -> None:
    settings = build_observability_settings(source(), service_name="gateway")

    assert settings.service_name == "gateway"
    assert settings.otlp_endpoint == "http://collector:4318"
    assert settings.signal_endpoint("traces") == "http://collector:4318/v1/traces"
    assert settings.signal_endpoint("metrics") == "http://collector:4318/v1/metrics"


def test_unknown_signal_is_rejected() -> None:
    settings = build_observability_settings(source(), service_name="gateway")

    with pytest.raises(ValueError, match="unsupported OTLP signal"):
        settings.signal_endpoint("logs")
