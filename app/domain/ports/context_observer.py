"""Framework-free, low-cardinality short-term context instrumentation."""

from typing import Literal, Protocol

ContextOperation = Literal["postgres_read", "postgres_write", "rewriter"]
RewriteOutcome = Literal["success", "error", "bypass"]
WriteOutcome = Literal["inserted", "duplicate", "error"]


class ContextObserverPort(Protocol):
    def context_observed(self, message_count: int, estimated_tokens: int) -> None: ...

    def rewrite_observed(self, outcome: RewriteOutcome, seconds: float | None) -> None: ...

    def degraded(
        self,
        correlation_id: str,
        operation: ContextOperation,
        error_class: str,
        fallback_mode: str,
    ) -> None: ...

    def conversation_write_observed(self, outcome: WriteOutcome) -> None: ...
