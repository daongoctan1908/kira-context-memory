"""Shared allowlist-only JSON logging for Gateway and Worker processes."""

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TextIO

from app.infrastructure.observability.context import current_context_fields
from app.infrastructure.observability.redaction import safe_log_value
from app.infrastructure.observability.tracing import current_trace_fields

_STRING_FIELDS = (
    "correlation_id",
    "turn_id",
    "event_id",
    "origin_trace_id",
    "operation",
    "dependency",
    "outcome",
    "error_class",
    "fallback_mode",
)
_NUMBER_FIELDS = ("attempt_count", "occurrence_count", "suppressed_count")

_TELEMETRY_EVENTS = frozenset(
    {
        "telemetry.export_failed",
        "telemetry.export_retrying",
        "telemetry.queue_dropped",
        "telemetry.collection_timeout",
        "telemetry.initialization_failed",
        "telemetry.shutdown_failed",
        "telemetry.shutdown_timeout",
    }
)
_ERROR_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# Exact SDK 1.44 templates: never interpolate arguments containing URLs or response bodies.
_SDK_EXPORT_MESSAGES = {
    "Failed to export span batch code: %s, reason: %s": "traces",
    "Failed to export span batch due to timeout, max retries or shutdown.": "traces",
    "Failed to export metrics batch code: %s, reason: %s": "metrics",
    "Failed to export metrics batch due to timeout, max retries or shutdown.": "metrics",
    "Exception while exporting metrics": "metrics",
}
_SDK_RETRY_MESSAGES = {
    "Transient error %s encountered while exporting span batch, retrying in %.2fs.": "traces",
    "Transient error %s encountered while exporting metrics batch, retrying in %.2fs.": "metrics",
}


class SafeJsonFormatter(logging.Formatter):
    """Emit required operational fields without formatting the log message or exception."""

    def __init__(
        self,
        *,
        service_name: str = "unknown",
        deployment_environment: str = "unknown",
    ) -> None:
        super().__init__()
        self._service_name = service_name
        self._deployment_environment = deployment_environment

    def format(self, record: logging.LogRecord) -> str:
        try:
            timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            )
        except Exception:
            timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")

        event = safe_log_value(getattr(record, "event", None)) or "application.log"
        payload: dict[str, object] = {
            "timestamp": timestamp,
            "severity": record.levelname,
            "event": event,
            "logger": record.name,
            "service.name": self._service_name,
            "deployment.environment": self._deployment_environment,
        }
        context_fields = current_context_fields()
        context_fields.update(current_trace_fields())

        for field in _STRING_FIELDS:
            value = getattr(record, field, context_fields.get(field))
            safe_value = safe_log_value(value)
            if safe_value is not None:
                payload[field] = safe_value
        for field in _NUMBER_FIELDS:
            safe_value = safe_log_value(getattr(record, field, None))
            if isinstance(safe_value, int | float) and not isinstance(safe_value, bool):
                payload[field] = safe_value
        for field in ("trace_id", "span_id"):
            safe_value = safe_log_value(context_fields.get(field))
            if isinstance(safe_value, str):
                payload[field] = safe_value

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class SafeTelemetryDiagnosticHandler(logging.StreamHandler):
    """Replace SDK diagnostics with finite, rate-limited operational facts.

    No SDK message, args, exception text or stack is ever formatted. Counters are local;
    they remain useful even when the collector that would receive metrics is unavailable.
    """

    kira_telemetry_handler = True

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(stream)
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._diagnostic_lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}
        self._suppressed: dict[tuple[str, str], int] = {}
        self._last_emitted: dict[tuple[str, str], float] = {}

    @property
    def diagnostic_counts(self) -> dict[tuple[str, str], int]:
        with self._diagnostic_lock:
            return dict(self._counts)

    def emit(self, record: logging.LogRecord) -> None:
        diagnostic = self._classify(record)
        if diagnostic is None:
            return
        event, signal, error_class = diagnostic
        key = (event, signal)
        now = self._clock()
        with self._diagnostic_lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            last = self._last_emitted.get(key)
            if last is not None and now - last < self._interval_seconds:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return
            suppressed = self._suppressed.pop(key, 0)
            self._last_emitted[key] = now

        safe_record = logging.LogRecord("opentelemetry", logging.WARNING, "", 0, "", (), None)
        safe_record.event = event
        safe_record.dependency = "otel"
        safe_record.operation = signal
        safe_record.outcome = "dropped" if event == "telemetry.queue_dropped" else "degraded"
        safe_record.error_class = error_class
        safe_record.occurrence_count = count
        safe_record.suppressed_count = suppressed
        super().emit(safe_record)

    @staticmethod
    def _classify(record: logging.LogRecord) -> tuple[str, str, str] | None:
        if record.name == "opentelemetry.kira":
            event = getattr(record, "event", None)
            signal = getattr(record, "operation", None)
            error_class = getattr(record, "error_class", None)
            if (
                type(event) is str
                and event in _TELEMETRY_EVENTS
                and type(signal) is str
                and signal in {"traces", "metrics", "runtime"}
            ):
                safe_error = (
                    error_class
                    if type(error_class) is str and _ERROR_CLASS.fullmatch(error_class)
                    else "TelemetryError"
                )
                return event, signal, safe_error
            return None
        if type(record.msg) is not str:
            return None
        if record.name.startswith("opentelemetry.exporter.otlp.proto.http."):
            if record.msg in _SDK_EXPORT_MESSAGES:
                return "telemetry.export_failed", _SDK_EXPORT_MESSAGES[record.msg], "ExportFailure"
            if record.msg in _SDK_RETRY_MESSAGES:
                return "telemetry.export_retrying", _SDK_RETRY_MESSAGES[record.msg], "ExportRetry"
        if record.name == "opentelemetry.sdk._shared_internal":
            if record.msg in {"Queue full, dropping %s.", "Exception while exporting %s."}:
                # SDK args contain its fixed signal label, never a provider response here.
                signal = "traces"
                if (
                    type(record.args) is tuple
                    and len(record.args) == 1
                    and type(record.args[0]) is str
                    and record.args[0] == "Metrics"
                ):
                    signal = "metrics"
                if record.msg == "Queue full, dropping %s.":
                    return "telemetry.queue_dropped", signal, "QueueFull"
                return "telemetry.export_failed", signal, "ExportFailure"
        if record.name == "opentelemetry.sdk.metrics._internal.export":
            if record.msg == "Exception while exporting metrics":
                return "telemetry.export_failed", "metrics", "ExportFailure"
            if record.msg in {
                "Metric collection timed out. Will try again after %s seconds",
                "Metric collection timed out.",
            }:
                return "telemetry.collection_timeout", "metrics", "MetricsTimeoutError"
        return None


def report_telemetry_diagnostic(
    event: str, *, signal: str = "runtime", error_class: str = "TelemetryError"
) -> None:
    """Report through the same safe SDK handler without exception strings or provider details."""
    try:
        logging.getLogger("opentelemetry.kira").warning(
            "", extra={"event": event, "operation": signal, "error_class": error_class}
        )
    except Exception:
        # A broken logging sink must not turn optional telemetry into a startup/business error.
        pass


def configure_structured_logging(
    namespace: str,
    *,
    level: str,
    service_name: str,
    deployment_environment: str,
) -> None:
    """Install exactly one reusable safe handler for an application logger namespace."""
    namespace_logger = logging.getLogger(namespace)
    namespace_logger.setLevel(level)
    marked_handlers = [
        handler
        for handler in namespace_logger.handlers
        if getattr(handler, "kira_safe_handler", False)
    ]
    if marked_handlers:
        handler = marked_handlers[0]
        for duplicate in marked_handlers[1:]:
            namespace_logger.removeHandler(duplicate)
    else:
        handler = logging.StreamHandler()
        handler.kira_safe_handler = True  # type: ignore[attr-defined]
        namespace_logger.addHandler(handler)
    handler.setLevel(level)
    handler.setFormatter(
        SafeJsonFormatter(
            service_name=service_name,
            deployment_environment=deployment_environment,
        )
    )
    namespace_logger.propagate = False

    # These dependencies may log full URLs, provider bodies, prompts, or credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("mem0").setLevel(logging.CRITICAL)
    logging.getLogger("mem0").propagate = False
    # Route SDK failures to a safe sink and stop raw third-party records reaching root handlers.
    telemetry_logger = logging.getLogger("opentelemetry")
    telemetry_logger.setLevel(logging.WARNING)
    telemetry_logger.propagate = False
    diagnostic_handler = next(
        (
            handler
            for handler in telemetry_logger.handlers
            if isinstance(handler, SafeTelemetryDiagnosticHandler)
        ),
        None,
    )
    if diagnostic_handler is None:
        diagnostic_handler = SafeTelemetryDiagnosticHandler()
    for existing in telemetry_logger.handlers[:]:
        telemetry_logger.removeHandler(existing)
    telemetry_logger.addHandler(diagnostic_handler)
    diagnostic_handler.setFormatter(
        SafeJsonFormatter(
            service_name=service_name,
            deployment_environment=deployment_environment,
        )
    )


def configure_app_logging(
    level: str,
    *,
    service_name: str = "kira-context-gateway",
    deployment_environment: str = "development",
) -> None:
    """Configure the Gateway logger namespace."""
    configure_structured_logging(
        "app",
        level=level,
        service_name=service_name,
        deployment_environment=deployment_environment,
    )
