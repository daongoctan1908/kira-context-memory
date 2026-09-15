"""Framework-free observation boundary for one memory-job attempt."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Literal, Protocol

from app.domain.ports.context_observer import StageObservationPort

MemoryJobStageKind = Literal["internal", "client"]
MemoryJobStageName = Literal[
    "conversation.read_boundary",
    "mem0.formation",
    "memory_job.transition",
]


class MemoryJobProcessObserverPort(Protocol):
    """Observe Worker substages without exposing an SDK to application code."""

    def stage(
        self,
        name: MemoryJobStageName,
        *,
        kind: MemoryJobStageKind = "internal",
        attributes: Mapping[str, object] | None = None,
    ) -> AbstractContextManager[StageObservationPort]: ...
