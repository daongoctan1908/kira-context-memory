import io
import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.logging import (
    SafeJsonFormatter,
    configure_structured_logging,
)


class DangerousValue:
    def __str__(self) -> str:
        raise RuntimeError("must not stringify")


def test_formatter_injects_app_and_trace_ids_without_merging_them() -> None:
    formatter = SafeJsonFormatter(
        service_name="kira-context-gateway",
        deployment_environment="test",
    )
    tracer = TracerProvider().get_tracer("test")
    record = logging.LogRecord("app.test", logging.INFO, "", 1, "private %s", ("secret",), None)
    record.event = "chat.received"

    with bind_observability_context(
        correlation_id="correlation-app",
        turn_id="turn-app",
        event_id="event-app",
    ):
        with tracer.start_as_current_span("test") as span:
            payload = json.loads(formatter.format(record))
            span_context = span.get_span_context()

    assert payload["correlation_id"] == "correlation-app"
    assert payload["turn_id"] == "turn-app"
    assert payload["event_id"] == "event-app"
    assert payload["trace_id"] == format(span_context.trace_id, "032x")
    assert payload["span_id"] == format(span_context.span_id, "016x")
    assert payload["correlation_id"] != payload["trace_id"]
    assert "private" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload)


def test_formatter_fails_closed_when_an_allowlisted_value_is_unsafe() -> None:
    record = logging.LogRecord("worker.test", logging.ERROR, "", 1, "private", (), None)
    record.event = DangerousValue()
    record.error_class = DangerousValue()
    record.exc_text = "private traceback"

    payload = SafeJsonFormatter().format(record)

    assert json.loads(payload)["event"] == "application.log"
    assert "private" not in payload
    assert "traceback" not in payload


def test_correlation_logging_stays_active_without_a_trace() -> None:
    record = logging.LogRecord("app.test", logging.INFO, "", 1, "private", (), None)
    with bind_observability_context(correlation_id="correlation-only"):
        payload = json.loads(SafeJsonFormatter().format(record))

    assert payload["correlation_id"] == "correlation-only"
    assert "trace_id" not in payload


def test_logging_configuration_is_idempotent_and_updates_the_formatter() -> None:
    namespace = "phase1_logging_test"
    logger = logging.getLogger(namespace)
    logger.handlers.clear()
    stream = io.StringIO()

    configure_structured_logging(
        namespace,
        level="INFO",
        service_name="service-one",
        deployment_environment="test",
    )
    configure_structured_logging(
        namespace,
        level="WARNING",
        service_name="service-two",
        deployment_environment="production",
    )
    handlers = [handler for handler in logger.handlers if handler.kira_safe_handler]
    assert len(handlers) == 1
    handlers[0].setStream(stream)

    logger.warning("not serialized", extra={"event": "runtime.ready"})
    payload = json.loads(stream.getvalue())

    assert payload["service.name"] == "service-two"
    assert payload["deployment.environment"] == "production"
    assert payload["event"] == "runtime.ready"
    assert logger.propagate is False
    logger.handlers.clear()
