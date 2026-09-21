import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
)
from app.domain.models.conversation import (
    ChatRequestReservation,
    ChatRequestReservationOutcome,
    ChatRequestStatus,
    ConversationListCursor,
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


async def test_conversation_management_operations_are_validated_and_forwarded() -> None:
    adapter = AsyncMock()
    created = object()
    listed = object()
    history = object()
    adapter.create_conversation.return_value = created
    adapter.list_conversations.return_value = listed
    adapter.read_history.return_value = history
    adapter.mark_deletion_pending.return_value = True
    adapter.purge_deletion_pending.return_value = True
    adapter.is_conversation_active.return_value = True
    reservation = object()
    adapter.reserve_chat_request.return_value = reservation
    adapter.abandon_chat_request.return_value = True
    completed = object()
    replayed = pair()
    adapter.complete_chat_request.return_value = completed
    adapter.read_completed_chat_request.return_value = replayed
    store = ManagedPostgresConversationStore(adapter, 1)
    cursor = ConversationListCursor(datetime(2026, 9, 19, tzinfo=UTC), uuid4())

    assert await store.create_conversation(USER_ID, title="Support") is created
    assert await store.list_conversations(USER_ID, limit=20, cursor=cursor) is listed
    assert (
        await store.read_history(USER_ID, "public-session", limit=50, before_message_id=42)
        is history
    )
    assert await store.mark_deletion_pending(USER_ID, "public-session")
    assert await store.purge_deletion_pending(USER_ID, "public-session")
    assert await store.is_conversation_active(USER_ID, "public-session")
    client_message_id = uuid4()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    assert (
        await store.reserve_chat_request(
            USER_ID,
            "public-session",
            client_message_id,
            b"h" * 32,
            now=now,
            lease_seconds=120,
        )
        is reservation
    )
    request_id, lease_token = uuid4(), uuid4()
    assert await store.abandon_chat_request(
        request_id,
        lease_token,
        status=ChatRequestStatus.CANCELLED,
        now=now,
    )
    owned = ChatRequestReservation(
        request_id,
        uuid4(),
        client_message_id,
        "turn-1",
        ChatRequestStatus.PROCESSING,
        ChatRequestReservationOutcome.ACQUIRED,
        1,
        lease_token,
        now,
    )
    completion_pair = pair()
    assert (
        await store.complete_chat_request(
            USER_ID,
            owned,
            *completion_pair,
            completed_at=now,
            schedule_memory=True,
        )
        is completed
    )
    replay_reservation = ChatRequestReservation(
        request_id,
        owned.conversation_id,
        client_message_id,
        "turn-1",
        ChatRequestStatus.COMPLETED,
        ChatRequestReservationOutcome.COMPLETED,
        1,
        None,
        None,
    )
    assert (
        await store.read_completed_chat_request(USER_ID, "public-session", replay_reservation)
        == replayed
    )

    adapter.validate_schema.assert_awaited_once()
    adapter.create_conversation.assert_awaited_once_with(USER_ID, title="Support")
    adapter.list_conversations.assert_awaited_once_with(
        USER_ID,
        limit=20,
        cursor=cursor,
        query=None,
    )
    adapter.read_history.assert_awaited_once_with(
        USER_ID,
        "public-session",
        limit=50,
        before_message_id=42,
    )
    adapter.mark_deletion_pending.assert_awaited_once_with(USER_ID, "public-session")
    adapter.purge_deletion_pending.assert_awaited_once_with(USER_ID, "public-session")
    adapter.is_conversation_active.assert_awaited_once_with(USER_ID, "public-session")
    adapter.reserve_chat_request.assert_awaited_once_with(
        USER_ID,
        "public-session",
        client_message_id,
        b"h" * 32,
        now=now,
        lease_seconds=120,
    )
    adapter.abandon_chat_request.assert_awaited_once_with(
        request_id,
        lease_token,
        status=ChatRequestStatus.CANCELLED,
        now=now,
    )
    adapter.complete_chat_request.assert_awaited_once_with(
        USER_ID,
        owned,
        *completion_pair,
        completed_at=now,
        schedule_memory=True,
        telemetry_context=None,
    )
    adapter.read_completed_chat_request.assert_awaited_once_with(
        USER_ID,
        "public-session",
        replay_reservation,
    )
