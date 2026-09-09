import json
import logging

from prometheus_client import generate_latest

from app.infrastructure.observability.context import ContextTelemetry, SafeJsonFormatter


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
    assert json.loads(SafeJsonFormatter().format(record)) == {
        "correlation_id": "correlation-1",
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
