import json
import logging

from app.infrastructure.observability.logging import SafeJsonFormatter
from tests.support.otel_metrics import gateway_telemetry


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


def test_metrics_have_bounded_labels_and_isolated_application_meters():
    telemetry, metrics = gateway_telemetry()
    telemetry.context_observed(2, 45)
    telemetry.memory_search_observed("success", 2, 0.03)
    telemetry.memory_job_schedule_observed("scheduled")
    telemetry.rewrite_observed("success", 0.12)
    telemetry.degraded("private-correlation", "rewriter", "PrivateError", "original_query")
    telemetry.conversation_write_observed("inserted")
    payload = repr(metrics.snapshot())
    assert "private-correlation" not in payload
    assert "PrivateError" not in payload
    assert "session_id" not in payload and "turn_id" not in payload
    assert metrics.value("kira.context.estimated_recent_tokens", field="sum") == 45
    assert metrics.value("kira.memory.search.count", {"outcome": "success"}) == 1
    assert metrics.value("kira.memory.search.result_count", field="sum") == 2
    assert metrics.value("kira.memory.search.duration", {"outcome": "success"}, field="count") == 1
    assert metrics.value("kira.memory.job.schedule.count", {"outcome": "scheduled"}) == 1
    _, separate = gateway_telemetry()
    assert separate.value("kira.memory.search.count") is None
    metrics.close()
    separate.close()


def test_memory_job_schedule_metric_has_only_bounded_outcome_label():
    telemetry, metrics = gateway_telemetry()
    for outcome in ("scheduled", "disabled", "duplicate", "error"):
        telemetry.memory_job_schedule_observed(outcome)

    payload = repr(metrics.snapshot())

    for outcome in ("scheduled", "disabled", "duplicate", "error"):
        assert metrics.value("kira.memory.job.schedule.count", {"outcome": outcome}) == 1
    for forbidden in ("user_id", "session_id", "turn_id", "event_id", "boundary_message_id"):
        assert forbidden not in payload
    metrics.close()


def test_memory_degradation_uses_mem0_dependency_without_sensitive_labels():
    telemetry, metrics = gateway_telemetry()
    telemetry.memory_search_observed("error", None, 0.5)
    telemetry.degraded(
        "private-correlation",
        "memory_search",
        "LongTermMemoryConnectionError",
        "recent_or_original_query",
    )

    payload = repr(metrics.snapshot())

    assert metrics.value("kira.memory.search.count", {"outcome": "error"}) == 1
    assert (
        metrics.value(
            "kira.context.degraded.count", {"dependency": "mem0", "operation": "memory_search"}
        )
        == 1
    )
    for forbidden in (
        "private-correlation",
        "LongTermMemoryConnectionError",
        "user_id",
        "session_id",
        "memory_id",
    ):
        assert forbidden not in payload
    metrics.close()


def test_search_emits_one_otel_outcome_without_sensitive_labels() -> None:
    telemetry, metrics = gateway_telemetry()

    telemetry.memory_search_observed("success", 2, 0.03)

    points = metrics.snapshot().points["kira.memory.search.count"]
    assert len(points) == 1
    assert points[0].attributes == {"outcome": "success"}
    assert points[0].value == 1
    metrics.close()


def test_memory_branch_metrics_record_outcome_count_and_latency() -> None:
    telemetry, metrics = gateway_telemetry()

    telemetry.memory_branch_observed("conversation", "success", 3, 0.04)
    telemetry.memory_branch_observed("global", "empty", 0, 0.01)
    telemetry.memory_branch_observed("global", "error", 0, 0.02)

    assert (
        metrics.value(
            "kira.memory.branch.duration", {"branch": "global", "outcome": "error"}, field="count"
        )
        == 1
    )
    assert (
        metrics.value("kira.memory.branch.result_count", {"branch": "conversation"}, field="sum")
        == 3
    )
    assert metrics.value("kira.memory.branch.result_count", {"branch": "global"}, field="sum") == 0
    points = {
        tuple(sorted(point.attributes.items())): point.value
        for point in metrics.snapshot().points["kira.memory.branch.count"]
    }
    assert points == {
        (("branch", "conversation"), ("outcome", "success")): 1,
        (("branch", "global"), ("outcome", "empty")): 1,
        (("branch", "global"), ("outcome", "error")): 1,
    }
    metrics.close()


def test_formation_scope_metrics_split_valid_fallback_and_invalid() -> None:
    telemetry, metrics = gateway_telemetry()

    telemetry.formation_scope_observed(conversation=2, global_count=1, fallback=1, invalid=1)

    points = {
        tuple(sorted(point.attributes.items())): point.value
        for point in metrics.snapshot().points["kira.memory.scope.count"]
    }
    assert points == {
        (("origin", "valid"), ("scope", "CONVERSATION")): 2,
        (("origin", "valid"), ("scope", "GLOBAL")): 1,
        (("origin", "fallback"), ("scope", "CONVERSATION")): 1,
        (("origin", "invalid"), ("scope", "unknown")): 1,
    }
    metrics.close()
