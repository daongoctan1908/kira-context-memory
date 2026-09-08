"""Framework-free, low-cardinality contextual instrumentation."""

from typing import Literal, Protocol

ContextOperation = Literal[
    "identity",
    "memory_search",
    "postgres_read",
    "postgres_write",
    "rewriter",
]
MemorySearchOutcome = Literal["success", "error", "bypass"]
RewriteOutcome = Literal["success", "error", "bypass"]
WriteOutcome = Literal["inserted", "duplicate", "error"]


class ContextObserverPort(Protocol):
    def context_observed(self, message_count: int, estimated_tokens: int) -> None: ...

    def memory_search_observed(
        self,
        outcome: MemorySearchOutcome,
        result_count: int | None,
        seconds: float | None,
    ) -> None: ...

    def rewrite_observed(self, outcome: RewriteOutcome, seconds: float | None) -> None: ...

    def degraded(
        self,
        correlation_id: str,
        operation: ContextOperation,
        error_class: str,
        fallback_mode: str,
    ) -> None: ...

    def conversation_write_observed(self, outcome: WriteOutcome) -> None: ...
