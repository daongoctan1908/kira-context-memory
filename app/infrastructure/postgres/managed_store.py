"""Runtime schema validation and dependency state without SDK leakage to core."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreError,
)
from app.domain.models.conversation import ConversationMessage
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter


class ManagedPostgresConversationStore:
    def __init__(self, adapter: PostgresConversationStoreAdapter, timeout_seconds: float) -> None:
        self._adapter = adapter
        self._timeout = timeout_seconds
        self._validation_lock = asyncio.Lock()
        self.status: Literal["initializing", "available", "degraded", "misconfigured"] = (
            "initializing"
        )

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(self._timeout):
                yield
        except ConversationStoreConfigurationError:
            self.status = "misconfigured"
            raise
        except TimeoutError:
            if self.status != "misconfigured":
                self.status = "degraded"
            raise ConversationStoreConnectionError() from None
        except ConversationStoreError:
            if self.status != "misconfigured":
                self.status = "degraded"
            raise

    async def validate_schema(self) -> None:
        async with self._operation(), self._validation_lock:
            if self.status != "available":
                await self._adapter.validate_schema()
                self.status = "available"

    async def read_recent(self, session_id: str, limit: int) -> tuple[ConversationMessage, ...]:
        async with self._operation():
            await self.validate_schema()
            return await self._adapter.read_recent(session_id, limit)

    async def append_turn(
        self,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
    ) -> bool:
        async with self._operation():
            await self.validate_schema()
            return await self._adapter.append_turn(user_message, assistant_message)
