"""Form long-term memory from an exact, persisted conversation boundary."""

from app.domain.errors.conversation import ConversationStoreProtocolError
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory import MemoryProcessResult, MemorySource
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.long_term_memory import LongTermMemoryPort


class ProcessMemoryUseCase:
    """Read a bounded persisted snapshot and delegate lifecycle decisions to Mem0."""

    def __init__(
        self,
        conversation_store: ConversationStorePort,
        long_term_memory: LongTermMemoryPort,
        *,
        message_limit: int = 10,
    ) -> None:
        if message_limit < 2 or message_limit % 2:
            raise ValueError("memory formation message limit must be a positive turn boundary")
        self._store = conversation_store
        self._memory = long_term_memory
        self._message_limit = message_limit

    async def execute(self, reference: CompletedTurnReference) -> MemoryProcessResult:
        """Process only messages owned by the user and ending at the referenced turn."""
        messages = await self._store.read_through_boundary(
            reference.user_id,
            reference.conversation_id,
            reference.boundary_message_id,
            self._message_limit,
        )
        try:
            source = MemorySource(reference, messages)
        except ValueError as error:
            # A missing, cross-session, or non-boundary snapshot violates the store contract.
            raise ConversationStoreProtocolError from error
        return await self._memory.process_memory(source)
