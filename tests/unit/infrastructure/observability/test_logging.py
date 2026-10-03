import io
import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.logging import (
    SafeJsonFormatter,
    SafeTelemetryDiagnosticHandler,
    configure_structured_logging,
    report_telemetry_diagnostic,
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
        origin_trace_id="origin-app",
    ):
        with tracer.start_as_current_span("test") as span:
            payload = json.loads(formatter.format(record))
            span_context = span.get_span_context()

    assert payload["correlation_id"] == "correlation-app"
    assert payload["turn_id"] == "turn-app"
    assert payload["event_id"] == "event-app"
    assert payload["origin_trace_id"] == "origin-app"
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


def test_sdk_diagnostics_are_rate_limited_without_formatting_sensitive_values() -> None:
    stream = io.StringIO()
    now = [0.0]
    handler = SafeTelemetryDiagnosticHandler(stream, clock=lambda: now[0])
    handler.setFormatter(SafeJsonFormatter())
    record = logging.LogRecord(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        logging.ERROR,
        "",
        0,
        "Failed to export span batch code: %s, reason: %s",
        (401, DangerousValue()),
        None,
    )
    record.exc_text = "private http://provider:4318?token=secret traceback"

    for _ in range(100):
        handler.handle(record)
    now[0] = 61.0
    handler.handle(record)

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(payloads) == 2
    assert payloads[0]["event"] == "telemetry.export_failed"
    assert payloads[0]["operation"] == "traces"
    assert payloads[1]["occurrence_count"] == 101
    assert payloads[1]["suppressed_count"] == 99
    assert handler.diagnostic_counts == {("telemetry.export_failed", "traces"): 101}
    assert "secret" not in stream.getvalue()
    assert "traceback" not in stream.getvalue()
    assert "provider" not in stream.getvalue()


def test_sdk_queue_drop_diagnostic_has_bounded_cardinality() -> None:
    stream = io.StringIO()
    handler = SafeTelemetryDiagnosticHandler(stream)
    handler.setFormatter(SafeJsonFormatter())
    record = logging.LogRecord(
        "opentelemetry.sdk._shared_internal",
        logging.WARNING,
        "",
        0,
        "Queue full, dropping %s.",
        ("Spans",),
        None,
    )

    for _ in range(100):
        handler.handle(record)

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "telemetry.queue_dropped"
    assert payload["error_class"] == "QueueFull"
    assert payload["outcome"] == "dropped"
    assert handler.diagnostic_counts == {("telemetry.queue_dropped", "traces"): 100}


def test_unknown_sdk_diagnostics_and_unsafe_internal_classes_are_not_serialized() -> None:
    stream = io.StringIO()
    handler = SafeTelemetryDiagnosticHandler(stream)
    handler.setFormatter(SafeJsonFormatter())
    unknown = logging.LogRecord(
        "opentelemetry.sdk.test", logging.ERROR, "", 0, DangerousValue(), (), None
    )
    unsafe = logging.LogRecord("opentelemetry.kira", logging.WARNING, "", 0, "", (), None)
    unsafe.event = "telemetry.shutdown_failed"
    unsafe.operation = "runtime"
    unsafe.error_class = "http://provider/private?token=secret"

    handler.handle(unknown)
    handler.handle(unsafe)

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "telemetry.shutdown_failed"
    assert payload["error_class"] == "TelemetryError"
    assert "secret" not in stream.getvalue()


def test_telemetry_logger_is_safe_and_reused_during_reconfiguration() -> None:
    configure_structured_logging(
        "test_telemetry_logging", level="INFO", service_name="one", deployment_environment="test"
    )
    telemetry_logger = logging.getLogger("opentelemetry")
    first_handler = telemetry_logger.handlers[0]
    configure_structured_logging(
        "test_telemetry_logging", level="INFO", service_name="two", deployment_environment="test"
    )

    assert telemetry_logger.handlers == [first_handler]
    assert isinstance(first_handler, SafeTelemetryDiagnosticHandler)
    assert telemetry_logger.propagate is False
    assert telemetry_logger.level == logging.WARNING


def test_diagnostic_sink_failure_remains_fail_open() -> None:
    class BrokenHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("broken sink secret")

    logger = logging.getLogger("opentelemetry.kira")
    original_handlers, original_propagate = logger.handlers[:], logger.propagate
    logger.handlers = [BrokenHandler()]
    logger.propagate = False
    try:
        report_telemetry_diagnostic("telemetry.export_failed", signal="traces")
    finally:
        logger.handlers = original_handlers
        logger.propagate = original_propagate
