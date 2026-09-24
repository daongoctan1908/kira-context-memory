import json
import logging

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from prometheus_client import generate_latest

from app.infrastructure.observability.context import ContextTelemetry
from app.infrastructure.observability.logging import SafeJsonFormatter


def test_structured_logs_exclude_content_secrets_and_exception_traces():
    record = logging.LogRecord(
        "app.test", logging.ERROR, "", 1, "private prompt %s", ("secret",), None
    )
    record.correlation_id = "correlation-1"
    record.operation = "postgres_write"
    record.dependency = "postgresql"
    record.error_class = "RuntimeError"
    record.fallback_mode = "answer_without_history"
    record.session_id = "private-session"
    record.turn_id = "private-turn"
    record.exc_text = "private credential traceback"
    payload = json.loads(
        SafeJsonFormatter(
            service_name="kira-context-gateway",
            deployment_environment="test",
        ).format(record)
    )
    assert payload == {
        "timestamp": payload["timestamp"],
        "severity": "ERROR",
        "event": "application.log",
        "logger": "app.test",
        "service.name": "kira-context-gateway",
        "deployment.environment": "test",
        "correlation_id": "correlation-1",
        "turn_id": "private-turn",
        "operation": "postgres_write",
        "dependency": "postgresql",
        "error_class": "RuntimeError",
        "fallback_mode": "answer_without_history",
    }


def test_metrics_have_bounded_labels_and_per_application_registry():
    telemetry = ContextTelemetry()
    telemetry.context_observed(2, 45)
    telemetry.memory_search_observed("success", 2, 0.03)
    telemetry.memory_job_schedule_observed("scheduled")
    telemetry.rewrite_observed("success", 0.12)
    telemetry.degraded("private-correlation", "rewriter", "PrivateError", "original_query")
    telemetry.conversation_write_observed("inserted")
    payload = generate_latest(telemetry.registry).decode()
    assert "private-correlation" not in payload
    assert "PrivateError" not in payload
    assert "session_id" not in payload and "turn_id" not in payload
    assert "kira_context_estimated_recent_tokens_sum 45.0" in payload
    assert 'kira_memory_search_total{outcome="success"} 1.0' in payload
    assert "kira_memory_search_results_sum 2.0" in payload
    assert 'kira_memory_search_duration_seconds_count{outcome="success"} 1.0' in payload
    assert 'kira_memory_job_schedule_total{outcome="scheduled"} 1.0' in payload
    assert ContextTelemetry().registry is not telemetry.registry


def test_memory_job_schedule_metric_has_only_bounded_outcome_label():
    telemetry = ContextTelemetry()
    for outcome in ("scheduled", "disabled", "duplicate", "error"):
        telemetry.memory_job_schedule_observed(outcome)

    payload = generate_latest(telemetry.registry).decode()

    for outcome in ("scheduled", "disabled", "duplicate", "error"):
        assert f'kira_memory_job_schedule_total{{outcome="{outcome}"}} 1.0' in payload
    for forbidden in ("user_id", "session_id", "turn_id", "event_id", "boundary_message_id"):
        assert forbidden not in payload


def test_memory_degradation_uses_mem0_dependency_without_sensitive_labels():
    telemetry = ContextTelemetry()
    telemetry.memory_search_observed("error", None, 0.5)
    telemetry.degraded(
        "private-correlation",
        "memory_search",
        "LongTermMemoryConnectionError",
        "recent_or_original_query",
    )

    payload = generate_latest(telemetry.registry).decode()

    assert 'kira_memory_search_total{outcome="error"} 1.0' in payload
    assert 'kira_context_degraded_total{dependency="mem0",operation="memory_search"} 1.0' in payload
    for forbidden in (
        "private-correlation",
        "LongTermMemoryConnectionError",
        "user_id",
        "session_id",
        "memory_id",
    ):
        assert forbidden not in payload


def test_phase5_dual_read_emits_equivalent_prometheus_and_otel_outcomes() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    telemetry = ContextTelemetry(meter=provider.get_meter("gateway-test"))

    telemetry.memory_search_observed("success", 2, 0.03)

    legacy = generate_latest(telemetry.registry).decode()
    data = reader.get_metrics_data()
    assert data is not None
    metrics = [
        metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    otel_count = next(metric for metric in metrics if metric.name == "kira.memory.search.count")

    assert 'kira_memory_search_total{outcome="success"} 1.0' in legacy
    assert otel_count.data.data_points[0].attributes == {"outcome": "success"}
    assert otel_count.data.data_points[0].value == 1
    provider.shutdown()


def test_memory_branch_metrics_record_outcome_count_and_latency() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    telemetry = ContextTelemetry(meter=provider.get_meter("gateway-test"))

    telemetry.memory_branch_observed("conversation", "success", 3, 0.04)
    telemetry.memory_branch_observed("global", "empty", 0, 0.01)
    telemetry.memory_branch_observed("global", "error", 0, 0.02)

    legacy = generate_latest(telemetry.registry).decode()
    assert 'kira_memory_branch_total{branch="conversation",outcome="success"} 1.0' in legacy
    assert 'kira_memory_branch_total{branch="global",outcome="empty"} 1.0' in legacy
    assert 'kira_memory_branch_total{branch="global",outcome="error"} 1.0' in legacy
    assert (
        'kira_memory_branch_duration_seconds_count{branch="global",outcome="error"} 1.0' in legacy
    )
    assert 'kira_memory_branch_results_sum{branch="conversation"} 3.0' in legacy
    assert 'kira_memory_branch_results_sum{branch="global"} 0.0' in legacy
    data = reader.get_metrics_data()
    assert data is not None
    metrics = [
        metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    otel_count = next(metric for metric in metrics if metric.name == "kira.memory.branch.count")
    points = {
        tuple(sorted(point.attributes.items())): point.value
        for point in otel_count.data.data_points
    }
    assert points == {
        (("branch", "conversation"), ("outcome", "success")): 1,
        (("branch", "global"), ("outcome", "empty")): 1,
        (("branch", "global"), ("outcome", "error")): 1,
    }
    provider.shutdown()


def test_formation_scope_metrics_split_valid_fallback_and_invalid() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    telemetry = ContextTelemetry(meter=provider.get_meter("gateway-test"))

    telemetry.formation_scope_observed(conversation=2, global_count=1, fallback=1, invalid=1)

    legacy = generate_latest(telemetry.registry).decode()
    assert 'kira_memory_scope_total{origin="valid",scope="CONVERSATION"} 2.0' in legacy
    assert 'kira_memory_scope_total{origin="valid",scope="GLOBAL"} 1.0' in legacy
    assert 'kira_memory_scope_total{origin="fallback",scope="CONVERSATION"} 1.0' in legacy
    assert 'kira_memory_scope_total{origin="invalid",scope="unknown"} 1.0' in legacy
    data = reader.get_metrics_data()
    assert data is not None
    metrics = [
        metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    otel_scope = next(metric for metric in metrics if metric.name == "kira.memory.scope.count")
    points = {
        tuple(sorted(point.attributes.items())): point.value
        for point in otel_scope.data.data_points
    }
    assert points == {
        (("origin", "valid"), ("scope", "CONVERSATION")): 2,
        (("origin", "valid"), ("scope", "GLOBAL")): 1,
        (("origin", "fallback"), ("scope", "CONVERSATION")): 1,
        (("origin", "invalid"), ("scope", "unknown")): 1,
    }
    provider.shutdown()
