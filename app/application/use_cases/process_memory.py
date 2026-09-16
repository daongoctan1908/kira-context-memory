"""Form long-term memory from an exact, persisted conversation boundary."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from app.domain.errors.conversation import ConversationStoreProtocolError
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory import MemoryProcessResult, MemorySource
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.long_term_memory import LongTermMemoryPort
from app.domain.ports.memory_job_observer import MemoryJobProcessObserverPort, MemoryJobStageName


class _NoOpObservation:
    def set_attribute(self, key: str, value: object) -> None:
        pass

    def set_outcome(self, outcome: str) -> None:
        pass

    def set_input(self, value: object) -> None:
        pass

    def set_output(self, value: object) -> None:
        pass


class ProcessMemoryUseCase:
    """Read a bounded persisted snapshot and delegate lifecycle decisions to Mem0."""

    def __init__(
        self,
        conversation_store: ConversationStorePort,
        long_term_memory: LongTermMemoryPort,
        *,
        message_limit: int = 10,
        observer: MemoryJobProcessObserverPort | None = None,
    ) -> None:
        if message_limit < 2 or message_limit % 2:
            raise ValueError("memory formation message limit must be a positive turn boundary")
        self._store = conversation_store
        self._memory = long_term_memory
        self._message_limit = message_limit
        self._observer = observer

    async def execute(
        self,
        reference: CompletedTurnReference,
        formation_event_id: UUID,
    ) -> MemoryProcessResult:
        """Process only messages owned by the user and ending at the referenced turn."""
        if not isinstance(formation_event_id, UUID):
            raise TypeError("formation_event_id must be a UUID")
        with self._stage(
            "conversation.read_boundary",
            kind="client",
            attributes={
                "kira.conversation.boundary_message_id": reference.boundary_message_id,
                "kira.memory.message_limit": self._message_limit,
            },
        ) as read_observation:
            try:
                messages = await self._store.read_through_boundary(
                    reference.user_id,
                    reference.conversation_id,
                    reference.boundary_message_id,
                    self._message_limit,
                )
                source = MemorySource(reference, messages, formation_event_id)
            except ValueError as error:
                _observe(read_observation.set_outcome, "error")
                raise ConversationStoreProtocolError from error
            except BaseException:
                _observe(read_observation.set_outcome, "error")
                raise
            _observe(read_observation.set_attribute, "kira.memory.message_count", len(messages))
            _observe(read_observation.set_outcome, "success")

        with self._stage(
            "mem0.formation",
            kind="client",
            attributes={"event_id": str(formation_event_id)},
        ) as formation_observation:
            _observe(
                formation_observation.set_input,
                [{"role": message.role.value, "content": message.content} for message in messages],
            )
            try:
                result = await self._memory.process_memory(source)
            except BaseException:
                _observe(formation_observation.set_outcome, "error")
                raise
            _observe(
                formation_observation.set_attribute,
                "kira.memory.lifecycle_event_count",
                len(result.events),
            )
            _observe(
                formation_observation.set_output,
                [
                    {
                        "event": event.action,
                        "memory": event.content,
                    }
                    for event in result.events
                ],
            )
            _observe(formation_observation.set_outcome, "success")
            return result

    @contextmanager
    def _stage(
        self,
        name: MemoryJobStageName,
        *,
        kind: str = "internal",
        attributes: dict[str, object] | None = None,
    ) -> Iterator[object]:
        if self._observer is None:
            yield _NoOpObservation()
            return
        try:
            manager = self._observer.stage(name, kind=kind, attributes=attributes)  # type: ignore[arg-type]
            observation = manager.__enter__()
        except Exception:
            yield _NoOpObservation()
            return
        try:
            yield observation
        except BaseException as error:
            try:
                manager.__exit__(type(error), error, error.__traceback__)
            except Exception:
                pass
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


def _observe(operation, *args: object) -> None:
    try:
        operation(*args)
    except Exception:
        pass
