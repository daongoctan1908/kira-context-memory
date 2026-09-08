"""Runtime schema validation and dependency state without SDK leakage to core."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreError,
)
from app.domain.models.conversation import AppendTurnResult, ConversationMessage
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

    async def read_recent(
        self,
        user_id: str,
        session_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        async with self._operation():
            await self.validate_schema()
            return await self._adapter.read_recent(user_id, session_id, limit)

    async def append_turn(
        self,
        user_id: str,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        *,
        schedule_memory: bool = False,
    ) -> AppendTurnResult:
        async with self._operation():
            await self.validate_schema()
            return await self._adapter.append_turn(
                user_id,
                user_message,
                assistant_message,
                schedule_memory=schedule_memory,
            )

    async def read_through_boundary(
        self,
        user_id: str,
        conversation_id: UUID,
        boundary_message_id: int,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        async with self._operation():
            await self.validate_schema()
            return await self._adapter.read_through_boundary(
                user_id,
                conversation_id,
                boundary_message_id,
                limit,
            )
