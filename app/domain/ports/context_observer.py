"""Framework-free, low-cardinality contextual instrumentation."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Literal, Protocol

ContextOperation = Literal[
    "identity",
    "memory_search",
    "postgres_read",
    "postgres_write",
    "rewriter",
]
MemorySearchOutcome = Literal["success", "error", "bypass"]
MemoryJobScheduleOutcome = Literal["scheduled", "disabled", "duplicate", "error"]
RewriteOutcome = Literal["success", "error", "bypass"]
WriteOutcome = Literal["inserted", "duplicate", "error"]
StageKind = Literal["internal", "client", "producer"]
StageName = Literal[
    "identity.resolve",
    "conversation.read_recent",
    "memory.search",
    "context.build",
    "rewrite.generate",
    "conversation.append_turn",
    "memory_job.enqueue",
]


class StageObservationPort(Protocol):
    """One request-local operation without exposing an observability SDK type."""

    def set_attribute(self, key: str, value: object) -> None: ...

    def set_outcome(self, outcome: str) -> None: ...


class ContextObserverPort(Protocol):
    def request_attribute(self, key: str, value: object) -> None: ...

    def stage(
        self,
        name: StageName,
        *,
        kind: StageKind = "internal",
        attributes: Mapping[str, object] | None = None,
    ) -> AbstractContextManager[StageObservationPort]: ...

    def context_observed(self, message_count: int, estimated_tokens: int) -> None: ...

    def memory_search_observed(
        self,
        outcome: MemorySearchOutcome,
        result_count: int | None,
        seconds: float | None,
    ) -> None: ...

    def memory_job_schedule_observed(self, outcome: MemoryJobScheduleOutcome) -> None: ...

    def rewrite_observed(self, outcome: RewriteOutcome, seconds: float | None) -> None: ...

    def degraded(
        self,
        correlation_id: str,
        operation: ContextOperation,
        error_class: str,
        fallback_mode: str,
    ) -> None: ...

    def conversation_write_observed(self, outcome: WriteOutcome) -> None: ...
