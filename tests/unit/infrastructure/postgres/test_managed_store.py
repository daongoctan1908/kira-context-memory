import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
)
from app.infrastructure.postgres.managed_store import ManagedPostgresConversationStore
from tests.support.context_fakes import pair

USER_ID = "user-1"


async def test_startup_outage_revalidates_before_recovering_and_writes():
    adapter = AsyncMock()
    adapter.validate_schema.side_effect = [ConversationStoreConnectionError(), None]
    adapter.read_recent.return_value = pair()
    result = object()
    adapter.append_turn.return_value = result
    store = ManagedPostgresConversationStore(adapter, 1)
    with pytest.raises(ConversationStoreConnectionError):
        await store.validate_schema()
    assert store.status == "degraded"
    assert await store.read_recent(USER_ID, "session-1", 10) == adapter.read_recent.return_value
    assert store.status == "available"
    assert await store.append_turn(USER_ID, *pair()) is result
    assert await store.append_turn(USER_ID, *pair(), schedule_memory=True) is result
    assert adapter.append_turn.await_args_list[-1].kwargs == {"schedule_memory": True}
    assert adapter.validate_schema.await_count == 2


async def test_runtime_schema_fault_stays_misconfigured_until_revalidated():
    adapter = AsyncMock()
    adapter.read_recent.side_effect = ConversationStoreConfigurationError()
    store = ManagedPostgresConversationStore(adapter, 1)
    await store.validate_schema()
    with pytest.raises(ConversationStoreConfigurationError):
        await store.read_recent(USER_ID, "session-1", 10)
    assert store.status == "misconfigured"
    adapter.validate_schema.side_effect = ConversationStoreConfigurationError()
    with pytest.raises(ConversationStoreConfigurationError):
        await store.append_turn(USER_ID, *pair())
    adapter.append_turn.assert_not_awaited()
    adapter.validate_schema.side_effect = ConversationStoreConnectionError()
    with pytest.raises(ConversationStoreConnectionError):
        await store.validate_schema()
    assert store.status == "misconfigured"
    adapter.validate_schema.side_effect = None
    await store.validate_schema()
    assert store.status == "available"


async def test_runtime_write_failure_is_transient():
    adapter = AsyncMock()
    adapter.append_turn.side_effect = ConversationStoreOperationError()
    store = ManagedPostgresConversationStore(adapter, 1)
    with pytest.raises(ConversationStoreOperationError):
        await store.append_turn(USER_ID, *pair())
    assert store.status == "degraded"


async def test_validation_has_total_deadline():
    adapter = AsyncMock()
    adapter.validate_schema.side_effect = asyncio.Event().wait
    store = ManagedPostgresConversationStore(adapter, 0.01)
    with pytest.raises(ConversationStoreConnectionError):
        await store.validate_schema()
    assert store.status == "degraded"


async def test_concurrent_first_reads_validate_once():
    adapter = AsyncMock()
    store = ManagedPostgresConversationStore(adapter, 1)
    await asyncio.gather(*(store.read_recent(USER_ID, "session-1", 10) for _ in range(5)))
    adapter.validate_schema.assert_awaited_once()


async def test_exact_boundary_read_is_forwarded() -> None:
    adapter = AsyncMock()
    expected = pair()
    adapter.read_through_boundary.return_value = expected
    store = ManagedPostgresConversationStore(adapter, 1)
    conversation_id = uuid4()

    assert await store.read_through_boundary(USER_ID, conversation_id, 42, 10) == expected
    adapter.read_through_boundary.assert_awaited_once_with(USER_ID, conversation_id, 42, 10)
