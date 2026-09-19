"""Shared allowlist-only JSON logging for Gateway and Worker processes."""

import json
import logging
from datetime import UTC, datetime

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
_NUMBER_FIELDS = ("attempt_count",)


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
    # Export failures are fail-open and must not create unbounded third-party stack-trace storms.
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)


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
