"""Per-application Prometheus registry and content-free structured logging."""

import json
import logging

from prometheus_client import CollectorRegistry, Counter, Histogram

from app.domain.ports.context_observer import (
    ContextOperation,
    MemorySearchOutcome,
    RewriteOutcome,
    WriteOutcome,
)

logger = logging.getLogger(__name__)


class SafeJsonFormatter(logging.Formatter):
    """Allowlist operational fields; never serialize messages or exception traces."""

    def format(self, record: logging.LogRecord) -> str:
        fields = ("correlation_id", "operation", "dependency", "error_class", "fallback_mode")
        return json.dumps(
            {field: getattr(record, field) for field in fields if hasattr(record, field)},
            ensure_ascii=False,
        )


def configure_app_logging(level: str) -> None:
    """Scope the safe handler to app loggers, leaving server logging independent."""
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not any(getattr(handler, "kira_safe_handler", False) for handler in app_logger.handlers):
        handler = logging.StreamHandler()
        handler.kira_safe_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(SafeJsonFormatter())
        app_logger.addHandler(handler)
    app_logger.propagate = False
    # HTTPX logs complete URLs at INFO; dependency details do not belong in app logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Mem0 upstream can log prompt/provider details. The application emits only
    # sanitized dependency outcomes at its own boundary.
    logging.getLogger("mem0").setLevel(logging.CRITICAL)
    logging.getLogger("mem0").propagate = False


class ContextTelemetry:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.recent_messages = Histogram(
            "kira_context_recent_messages",
            "Messages retained after context trimming",
            buckets=(0, 2, 4, 6, 8, 10, 20),
            registry=self.registry,
        )
        self.recent_tokens = Histogram(
            "kira_context_estimated_recent_tokens",
            "Estimated tokens, not exact Qwen tokens",
            buckets=(0, 100, 500, 1000, 2000, 3000, 6000),
            registry=self.registry,
        )
        self.memory_searches = Counter(
            "kira_memory_search_total",
            "Long-term-memory search outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.memory_search_latency = Histogram(
            "kira_memory_search_duration_seconds",
            "Attempted long-term-memory search latency",
            ["outcome"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 3, 5),
            registry=self.registry,
        )
        self.memory_search_results = Histogram(
            "kira_memory_search_results",
            "Ranked memories returned by successful searches",
            buckets=(0, 1, 2, 3, 5, 10),
            registry=self.registry,
        )
        self.rewrites = Counter(
            "kira_context_rewrite_total",
            "Rewrite outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.rewrite_latency = Histogram(
            "kira_context_rewrite_duration_seconds",
            "Attempted rewrite latency",
            ["outcome"],
            buckets=(0.05, 0.1, 0.5, 1, 2, 4, 8, 10),
            registry=self.registry,
        )
        self.degradations = Counter(
            "kira_context_degraded_total",
            "Contextual dependency failures",
            ["dependency", "operation"],
            registry=self.registry,
        )
        self.writes = Counter(
            "kira_conversation_write_total",
            "Completed turn write outcomes",
            ["outcome"],
            registry=self.registry,
        )

    def context_observed(self, message_count: int, estimated_tokens: int) -> None:
        self.recent_messages.observe(message_count)
        self.recent_tokens.observe(estimated_tokens)

    def memory_search_observed(
        self,
        outcome: MemorySearchOutcome,
        result_count: int | None,
        seconds: float | None,
    ) -> None:
        self.memory_searches.labels(outcome).inc()
        if seconds is not None:
            self.memory_search_latency.labels(outcome).observe(seconds)
        if result_count is not None:
            self.memory_search_results.observe(result_count)

    def rewrite_observed(self, outcome: RewriteOutcome, seconds: float | None) -> None:
        self.rewrites.labels(outcome).inc()
        if seconds is not None:
            self.rewrite_latency.labels(outcome).observe(seconds)

    def degraded(
        self,
        correlation_id: str,
        operation: ContextOperation,
        error_class: str,
        fallback_mode: str,
    ) -> None:
        dependency = {
            "identity": "identity",
            "memory_search": "mem0",
            "rewriter": "vllm",
        }.get(operation, "postgresql")
        self.degradations.labels(dependency, operation).inc()
        logger.warning(
            "Context capability degraded",
            extra={
                "correlation_id": correlation_id,
                "operation": operation,
                "dependency": dependency,
                "error_class": error_class,
                "fallback_mode": fallback_mode,
            },
        )

    def conversation_write_observed(self, outcome: WriteOutcome) -> None:
        self.writes.labels(outcome).inc()
