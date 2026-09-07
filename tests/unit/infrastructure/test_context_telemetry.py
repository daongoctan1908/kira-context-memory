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
    telemetry.rewrite_observed("success", 0.12)
    telemetry.degraded("private-correlation", "rewriter", "PrivateError", "original_query")
    telemetry.conversation_write_observed("inserted")
    payload = generate_latest(telemetry.registry).decode()
    assert "private-correlation" not in payload
    assert "PrivateError" not in payload
    assert "session_id" not in payload and "turn_id" not in payload
    assert "kira_context_estimated_recent_tokens_sum 45.0" in payload
    assert ContextTelemetry().registry is not telemetry.registry
