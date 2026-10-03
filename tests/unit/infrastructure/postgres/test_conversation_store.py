from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.domain.errors.conversation import (
    ChatRequestConflictError,
    ConversationSourceUnavailableError,
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.models.conversation import (
    ChatRequestReservationOutcome,
    ChatRequestStatus,
    ConversationListCursor,
    ConversationMessage,
    ConversationRole,
    ConversationStatus,
    MessageFeedbackRating,
)
from app.infrastructure.postgres.conversation_store import (
    PostgresConversationStoreAdapter,
    _find_sqlstate,
    _is_connection,
)
from app.infrastructure.postgres.schema import EXPECTED_SCHEMA_REVISION, PREVIOUS_SCHEMA_REVISION

USER_ID = "user-1"


class FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one(self) -> dict[str, Any]:
        if len(self._rows) != 1:
            raise AssertionError("expected exactly one row")
        return self._rows[0]

    def one_or_none(self) -> dict[str, Any] | None:
        if len(self._rows) > 1:
            raise AssertionError("expected zero or one row")
        return self._rows[0] if self._rows else None


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        one: object | None = None,
        rowcount: int = 0,
    ) -> None:
        self._rows = rows or []
        self._one = one
        self.rowcount = rowcount

    def mappings(self) -> FakeMappings:
        return FakeMappings(self._rows)

    def one_or_none(self) -> object | None:
        return self._one


class FakeConnection:
    def __init__(
        self,
        results: list[FakeResult | None] | None = None,
        *,
        scalar: object = EXPECTED_SCHEMA_REVISION,
        error: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.scalar_value = scalar
        self.error = error
        self.calls: list[tuple[object, object | None]] = []

    async def execute(self, statement: object, parameters: object | None = None) -> FakeResult:
        self.calls.append((statement, parameters))
        if self.error is not None:
            raise self.error
        result = self.results.pop(0) if self.results else None
        return result or FakeResult()

    async def scalar(self, statement: object) -> object:
        self.calls.append((statement, None))
        if self.error is not None:
            raise self.error
        return self.scalar_value


class FakeContext:
    def __init__(self, connection: FakeConnection, *, enter_error: Exception | None = None) -> None:
        self.connection = connection
        self.enter_error = enter_error

    async def __aenter__(self) -> FakeConnection:
        if self.enter_error is not None:
            raise self.enter_error
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeEngine:
    def __init__(
        self,
        connection: FakeConnection,
        *,
        enter_error: Exception | None = None,
    ) -> None:
        self.connection = connection
        self.enter_error = enter_error

    def connect(self) -> FakeContext:
        return FakeContext(self.connection, enter_error=self.enter_error)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection, enter_error=self.enter_error)


def message(
    role: ConversationRole,
    *,
    session_id: str = "session-1",
    turn_id: str = "turn-1",
    content: str | None = None,
) -> ConversationMessage:
    return ConversationMessage(
        session_id=session_id,
        turn_id=turn_id,
        role=role,
        content=content or role.value,
        timestamp=datetime(2026, 9, 3, 8, 30, tzinfo=UTC),
    )


def adapter(connection: FakeConnection, *, enter_error: Exception | None = None):
    return PostgresConversationStoreAdapter(
        FakeEngine(connection, enter_error=enter_error)  # type: ignore[arg-type]
    )


async def test_validate_schema_accepts_expected_revision() -> None:
    connection = FakeConnection()

    await adapter(connection).validate_schema()

    assert len(connection.calls) == 1


async def test_validate_schema_accepts_explicit_bridge_revision() -> None:
    store = adapter(FakeConnection(scalar=PREVIOUS_SCHEMA_REVISION))

    await store.validate_schema()

    assert store._schema_revision == PREVIOUS_SCHEMA_REVISION


async def test_validate_schema_rejects_wrong_revision() -> None:
    with pytest.raises(ConversationStoreConfigurationError):
        await adapter(FakeConnection(scalar="old-revision")).validate_schema()


async def test_validate_schema_maps_connection_timeout() -> None:
    with pytest.raises(ConversationStoreConnectionError):
        await adapter(
            FakeConnection(), enter_error=SqlAlchemyTimeoutError("unavailable")
        ).validate_schema()


def _conversation_row(
    *,
    conversation_id=None,
    session_id: str = "public-session",
    title: str | None = "Support",
    status: str = "active",
    created_at: datetime | None = None,
    last_message_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = created_at or datetime(2026, 9, 19, tzinfo=UTC)
    return {
        "conversation_id": conversation_id or uuid4(),
        "session_id": session_id,
        "title": title,
        "status": status,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_message_at": last_message_at,
    }


async def test_create_conversation_generates_public_id_and_normalizes_title() -> None:
    row = _conversation_row(title="Support request")
    connection = FakeConnection([FakeResult(rows=[row])])
    store = adapter(connection)
    await store.validate_schema()

    result = await store.create_conversation(USER_ID, title="  Support request  ")

    assert result.session_id == "public-session"
    assert result.status is ConversationStatus.ACTIVE
    statement = connection.calls[1][0]
    params = statement.compile().params
    assert params["title"] == "Support request"
    assert len(params["session_id"]) == 32
    assert params["session_id"] != "public-session"


async def test_feedback_requires_current_schema_and_management_input_is_bounded() -> None:
    store = adapter(FakeConnection(scalar=PREVIOUS_SCHEMA_REVISION))
    await store.validate_schema()
    with pytest.raises(ConversationStoreConfigurationError):
        await store.set_message_feedback(
            USER_ID,
            "public-session",
            "turn-1",
            MessageFeedbackRating.UP,
        )

    current = adapter(FakeConnection())
    await current.validate_schema()
    with pytest.raises(ValueError):
        await current.create_conversation("", title="valid")
    with pytest.raises(ValueError):
        await current.create_conversation(USER_ID, title=" ")
    with pytest.raises(ValueError):
        await current.list_conversations(USER_ID, limit=101)
    with pytest.raises(ValueError):
        await current.read_history(USER_ID, "public-session", limit=10, before_message_id=0)


async def test_list_conversations_uses_matching_keyset_order_and_cursor() -> None:
    newest = datetime(2026, 9, 19, 3, tzinfo=UTC)
    rows = [
        _conversation_row(session_id="one", last_message_at=newest),
        _conversation_row(session_id="two", created_at=datetime(2026, 9, 19, 2, tzinfo=UTC)),
        _conversation_row(session_id="lookahead"),
    ]
    connection = FakeConnection([FakeResult(rows=rows)])
    store = adapter(connection)
    await store.validate_schema()

    page = await store.list_conversations(USER_ID, limit=2)

    assert [item.session_id for item in page.items] == ["one", "two"]
    assert page.next_cursor == ConversationListCursor(
        page.items[-1].activity_at,
        page.items[-1].conversation_id,
    )
    sql = str(connection.calls[1][0])
    assert "coalesce(conversations.last_message_at, conversations.created_at) DESC" in sql
    assert "conversations.conversation_id DESC" in sql
    assert "conversations.user_id" in sql

    next_connection = FakeConnection([FakeResult(rows=[])])
    next_store = adapter(next_connection)
    await next_store.validate_schema()
    await next_store.list_conversations(USER_ID, limit=2, cursor=page.next_cursor)
    cursor_sql = str(next_connection.calls[1][0])
    assert "coalesce(conversations.last_message_at, conversations.created_at) <" in cursor_sql
    assert "conversations.conversation_id <" in cursor_sql


async def test_read_history_is_chronological_paginated_and_owned() -> None:
    rows = [
        {
            "message_id": message_id,
            "turn_id": f"turn-{(message_id + 1) // 2}",
            "role": role,
            "content": role,
            "message_timestamp": datetime(2026, 9, 19, 0, message_id, tzinfo=UTC),
            "schema_version": 1,
            "turn_sequence": (message_id + 1) // 2,
            "message_index": 0 if role == "user" else 1,
        }
        for message_id, role in ((4, "assistant"), (3, "user"), (2, "assistant"))
    ]
    connection = FakeConnection([FakeResult(rows=rows)])
    store = adapter(connection)
    await store.validate_schema()
    connection.scalar_value = uuid4()

    page = await store.read_history(USER_ID, "public-session", limit=2)

    assert page is not None
    assert [message.role for message in page.messages] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert page.next_before_message_id == 3
    assert "conversations.status" in str(connection.calls[1][0])
    assert "conversation_messages.turn_sequence DESC" in str(connection.calls[2][0])

    missing_connection = FakeConnection()
    missing = adapter(missing_connection)
    await missing.validate_schema()
    missing_connection.scalar_value = None
    assert await missing.read_history(USER_ID, "missing", limit=2) is None


async def test_mark_deletion_pending_is_owned_and_idempotent() -> None:
    conversation_id = uuid4()
    connection = FakeConnection(
        [FakeResult(one=SimpleNamespace(conversation_id=conversation_id)), FakeResult()]
    )
    store = adapter(connection)
    await store.validate_schema()

    assert await store.mark_deletion_pending(USER_ID, "public-session")
    select_sql = str(connection.calls[1][0])
    update_sql = str(connection.calls[2][0])
    assert "conversations.user_id" in select_sql
    assert "conversations.session_id" in select_sql
    assert "FOR UPDATE" in select_sql
    assert "UPDATE conversations" in update_sql
    assert connection.calls[2][0].compile().params["status"] == "deletion_pending"

    missing = adapter(FakeConnection([FakeResult(one=None)]))
    await missing.validate_schema()
    assert not await missing.mark_deletion_pending(USER_ID, "missing")


async def test_purge_deletion_pending_requires_owned_pending_row_and_deletes_once() -> None:
    conversation_id = uuid4()
    connection = FakeConnection([FakeResult(rowcount=1)])
    store = adapter(connection)
    await store.validate_schema()
    connection.scalar_value = conversation_id

    assert await store.purge_deletion_pending(USER_ID, "public-session")
    lock_sql = str(connection.calls[1][0])
    delete_sql = str(connection.calls[2][0])
    assert "conversations.status" in lock_sql
    assert "FOR UPDATE" in lock_sql
    assert "DELETE FROM conversations" in delete_sql

    missing_connection = FakeConnection()
    missing = adapter(missing_connection)
    await missing.validate_schema()
    missing_connection.scalar_value = None
    assert not await missing.purge_deletion_pending(USER_ID, "missing")


def test_memory_table_identifiers_are_validated_at_adapter_construction() -> None:
    with pytest.raises(ValueError, match="identifier"):
        PostgresConversationStoreAdapter(  # type: ignore[arg-type]
            FakeEngine(FakeConnection()),
            memory_enabled=True,
            memory_schema='memory"; DROP SCHEMA public; --',
        )


async def test_active_conversation_check_requires_owner_and_active_status() -> None:
    conversation_id = uuid4()
    connection = FakeConnection()
    store = adapter(connection)
    await store.validate_schema()
    connection.scalar_value = conversation_id

    assert await store.is_conversation_active(USER_ID, "public-session")
    statement = connection.calls[1][0]
    compiled = statement.compile()
    assert "conversations.user_id" in str(statement)
    assert "conversations.session_id" in str(statement)
    assert "conversations.status" in str(statement)
    assert compiled.params["status_1"] == ConversationStatus.ACTIVE.value

    connection.scalar_value = None
    assert not await store.is_conversation_active(USER_ID, "public-session")


def _chat_request_row(
    *,
    status: str = "processing",
    content_hash: bytes = b"h" * 32,
    attempt_count: int = 1,
    lease_expires_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "request_id": uuid4(),
        "conversation_id": uuid4(),
        "client_message_id": uuid4(),
        "content_hash": content_hash,
        "turn_id": "turn-request",
        "status": status,
        "attempt_count": attempt_count,
        "lease_token": uuid4() if status == "processing" else None,
        "lease_expires_at": lease_expires_at if status == "processing" else None,
        "created_at": datetime(2026, 9, 19, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 19, tzinfo=UTC),
        "completed_at": datetime(2026, 9, 19, tzinfo=UTC) if status == "completed" else None,
    }


async def test_reserve_chat_request_acquires_new_fenced_attempt() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    conversation_id = uuid4()
    inserted = _chat_request_row(lease_expires_at=now + timedelta(minutes=2))
    inserted["conversation_id"] = conversation_id
    client_message_id = inserted["client_message_id"]
    connection = FakeConnection(
        [
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id)),
            FakeResult(rows=[]),
            FakeResult(rows=[]),
            FakeResult(rows=[inserted]),
        ]
    )
    store = adapter(connection)
    await store.validate_schema()

    reservation = await store.reserve_chat_request(
        USER_ID,
        "public-session",
        client_message_id,
        b"h" * 32,
        now=now,
        lease_seconds=120,
    )

    assert reservation is not None
    assert reservation.outcome is ChatRequestReservationOutcome.ACQUIRED
    assert reservation.lease_token == inserted["lease_token"]
    assert "FOR UPDATE" in str(connection.calls[1][0])
    assert "INSERT INTO chat_requests" in str(connection.calls[4][0])


async def test_duplicate_request_detects_conflict_in_progress_and_completed() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    conversation_id = uuid4()
    active = _chat_request_row(lease_expires_at=now + timedelta(minutes=1))
    active["conversation_id"] = conversation_id

    for expected_status, expected_outcome in (
        ("processing", ChatRequestReservationOutcome.IN_PROGRESS),
        ("completed", ChatRequestReservationOutcome.COMPLETED),
    ):
        existing = dict(active)
        existing.update(
            status=expected_status,
            lease_token=active["lease_token"] if expected_status == "processing" else None,
            lease_expires_at=active["lease_expires_at"]
            if expected_status == "processing"
            else None,
            completed_at=now if expected_status == "completed" else None,
        )
        connection = FakeConnection(
            [
                FakeResult(one=SimpleNamespace(conversation_id=conversation_id)),
                FakeResult(rows=[existing]),
            ]
        )
        store = adapter(connection)
        await store.validate_schema()
        reservation = await store.reserve_chat_request(
            USER_ID,
            "public-session",
            existing["client_message_id"],
            b"h" * 32,
            now=now,
            lease_seconds=120,
        )
        assert reservation is not None
        assert reservation.outcome is expected_outcome
        assert reservation.lease_token is None

    conflict_connection = FakeConnection(
        [
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id)),
            FakeResult(rows=[active]),
        ]
    )
    conflict_store = adapter(conflict_connection)
    await conflict_store.validate_schema()
    with pytest.raises(ChatRequestConflictError):
        await conflict_store.reserve_chat_request(
            USER_ID,
            "public-session",
            active["client_message_id"],
            b"x" * 32,
            now=now,
            lease_seconds=120,
        )


async def test_expired_or_failed_request_is_reclaimed_with_new_attempt() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    conversation_id = uuid4()
    expired = _chat_request_row(lease_expires_at=now - timedelta(seconds=1))
    expired["conversation_id"] = conversation_id
    reclaimed = dict(expired)
    reclaimed.update(
        status="processing",
        attempt_count=2,
        lease_token=uuid4(),
        lease_expires_at=now + timedelta(minutes=2),
    )
    connection = FakeConnection(
        [
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id)),
            FakeResult(rows=[expired]),
            FakeResult(rows=[reclaimed]),
        ]
    )
    store = adapter(connection)
    await store.validate_schema()

    reservation = await store.reserve_chat_request(
        USER_ID,
        "public-session",
        expired["client_message_id"],
        b"h" * 32,
        now=now,
        lease_seconds=120,
    )
    assert reservation is not None
    assert reservation.outcome is ChatRequestReservationOutcome.RECLAIMED
    assert reservation.attempt_count == 2
    assert reservation.lease_token == reclaimed["lease_token"]


async def test_other_processing_request_blocks_new_reservation_without_exposing_lease() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    conversation_id = uuid4()
    processing = _chat_request_row(lease_expires_at=now + timedelta(minutes=1))
    processing["conversation_id"] = conversation_id
    connection = FakeConnection(
        [
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id)),
            FakeResult(rows=[]),
            FakeResult(rows=[processing]),
        ]
    )
    store = adapter(connection)
    await store.validate_schema()

    result = await store.reserve_chat_request(
        USER_ID,
        "public-session",
        uuid4(),
        b"n" * 32,
        now=now,
        lease_seconds=120,
    )
    assert result is not None
    assert result.outcome is ChatRequestReservationOutcome.IN_PROGRESS
    assert result.lease_token is None


async def test_abandon_chat_request_fences_stale_attempt_token() -> None:
    connection = FakeConnection([FakeResult(rowcount=1)])
    store = adapter(connection)
    await store.validate_schema()
    now = datetime(2026, 9, 19, tzinfo=UTC)

    assert await store.abandon_chat_request(
        uuid4(),
        uuid4(),
        status=ChatRequestStatus.FAILED,
        now=now,
    )
    statement = connection.calls[1][0]
    sql = str(statement)
    assert "chat_requests.lease_token" in sql
    assert "chat_requests.status" in sql
    assert statement.compile().params["status"] == "failed"

    stale = adapter(FakeConnection([FakeResult(rowcount=0)]))
    await stale.validate_schema()
    assert not await stale.abandon_chat_request(
        uuid4(),
        uuid4(),
        status=ChatRequestStatus.CANCELLED,
        now=now,
    )


@pytest.mark.parametrize(
    "outcome,attempt_count",
    [
        (ChatRequestReservationOutcome.ACQUIRED, 1),
        (ChatRequestReservationOutcome.RECLAIMED, 2),
    ],
)
async def test_completion_stores_first_request_timestamp_for_user(
    outcome: ChatRequestReservationOutcome,
    attempt_count: int,
) -> None:
    source_time = datetime(2026, 9, 19, tzinfo=UTC)
    attempt_time = source_time + timedelta(minutes=10)
    user = replace(message(ConversationRole.USER), timestamp=attempt_time)
    assistant = replace(
        message(ConversationRole.ASSISTANT), timestamp=attempt_time + timedelta(seconds=1)
    )
    request = _chat_request_row(
        content_hash=sha256(user.content.encode()).digest(),
        attempt_count=attempt_count,
        lease_expires_at=attempt_time + timedelta(minutes=2),
    )
    request["turn_id"] = user.turn_id
    reservation = PostgresConversationStoreAdapter._to_chat_reservation(
        request, outcome, expose_lease=True
    )
    connection = FakeConnection(
        [
            FakeResult(
                one=SimpleNamespace(
                    conversation_id=reservation.conversation_id,
                    session_id=user.session_id,
                    next_turn_sequence=1,
                )
            ),
            FakeResult(rows=[request]),
            FakeResult(rowcount=1),
            FakeResult(
                rows=[
                    {"message_id": 10, "message_index": 0},
                    {"message_id": 11, "message_index": 1},
                ]
            ),
            FakeResult(rowcount=1),
        ]
    )
    store = adapter(connection)
    await store.validate_schema()

    result = await store.complete_chat_request(
        USER_ID,
        reservation,
        user,
        assistant,
        completed_at=attempt_time + timedelta(seconds=2),
    )

    assert result.inserted
    stored_pair = connection.calls[4][1]
    assert stored_pair[0]["message_timestamp"] == source_time
    assert stored_pair[1]["message_timestamp"] == assistant.timestamp


async def test_read_recent_returns_chronological_domain_messages() -> None:
    rows = [
        {
            "turn_id": "turn-1",
            "role": "user",
            "content": "Xin chào",
            "message_timestamp": datetime(2026, 9, 3, 8, 30, tzinfo=UTC),
            "schema_version": 1,
            "turn_sequence": 1,
            "message_index": 0,
        },
        {
            "turn_id": "turn-1",
            "role": "assistant",
            "content": "Chào bạn",
            "message_timestamp": datetime(2026, 9, 3, 8, 31, tzinfo=UTC),
            "schema_version": 1,
            "turn_sequence": 1,
            "message_index": 1,
        },
    ]
    connection = FakeConnection([FakeResult(rows=rows)])

    result = await adapter(connection).read_recent(USER_ID, "session-1", 10)

    assert [item.role for item in result] == [ConversationRole.USER, ConversationRole.ASSISTANT]
    assert [item.content for item in result] == ["Xin chào", "Chào bạn"]
    assert all(item.session_id == "session-1" for item in result)
    assert "LIMIT" in str(connection.calls[0][0])
    assert "conversations.user_id" in str(connection.calls[0][0])


async def test_read_through_boundary_requires_owned_assistant_and_orders_messages() -> None:
    rows = [
        {
            "turn_id": "turn-1",
            "role": role,
            "content": role,
            "message_timestamp": datetime(2026, 9, 3, 8, index, tzinfo=UTC),
            "schema_version": 1,
            "turn_sequence": 1,
            "message_index": index,
        }
        for index, role in enumerate(("user", "assistant"))
    ]
    connection = FakeConnection([FakeResult(rows=rows)], scalar="session-1")

    result = await adapter(connection).read_through_boundary(USER_ID, uuid4(), 42, 10)

    assert [message.role for message in result] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert "message_id <=" in str(connection.calls[1][0])


async def test_read_through_boundary_rejects_unowned_boundary() -> None:
    with pytest.raises(ConversationSourceUnavailableError):
        await adapter(FakeConnection(scalar=None)).read_through_boundary(USER_ID, uuid4(), 42, 10)


async def test_read_through_boundary_requires_active_source() -> None:
    connection = FakeConnection()
    store = adapter(connection)
    await store.validate_schema()
    connection.scalar_value = None

    with pytest.raises(ConversationSourceUnavailableError):
        await store.read_through_boundary(USER_ID, uuid4(), 42, 10)

    assert "conversations.status" in str(connection.calls[1][0])


@pytest.mark.parametrize(
    "user_id,session_id,limit",
    [("", "session-1", 1), (USER_ID, "", 1), (USER_ID, "session-1", 0)],
)
async def test_read_recent_validates_arguments(user_id: str, session_id: str, limit: int) -> None:
    with pytest.raises(ValueError):
        await adapter(FakeConnection()).read_recent(user_id, session_id, limit)


async def test_read_recent_maps_malformed_row() -> None:
    connection = FakeConnection([FakeResult(rows=[{"message_timestamp": "not-a-date"}])])

    with pytest.raises(ConversationStoreProtocolError):
        await adapter(connection).read_recent(USER_ID, "session-1", 10)


async def test_read_recent_maps_database_failure() -> None:
    connection = FakeConnection(error=IntegrityError("statement", {}, Exception("failure")))

    with pytest.raises(ConversationStoreOperationError):
        await adapter(connection).read_recent(USER_ID, "session-1", 10)


async def test_append_turn_inserts_atomic_pair_and_advances_sequence() -> None:
    conversation_id = uuid4()
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id, next_turn_sequence=3)),
            FakeResult(rows=[]),
            FakeResult(
                rows=[
                    {"message_id": 10, "message_index": 0},
                    {"message_id": 11, "message_index": 1},
                ]
            ),
            FakeResult(),
        ]
    )

    result = await adapter(connection).append_turn(
        USER_ID,
        message(ConversationRole.USER, content="Câu hỏi"),
        message(ConversationRole.ASSISTANT, content="Trả lời"),
    )

    assert result.inserted is True
    assert result.reference.boundary_message_id == 11
    assert result.memory_job_event_id is None
    assert result.reference.user_id == USER_ID
    assert len(connection.calls) == 5
    pair = connection.calls[3][1]
    assert isinstance(pair, list)
    assert [item["message_index"] for item in pair] == [0, 1]
    assert [item["turn_sequence"] for item in pair] == [3, 3]
    assert [item["content"] for item in pair] == ["Câu hỏi", "Trả lời"]


async def test_append_turn_schedules_reference_only_memory_job_in_same_transaction() -> None:
    conversation_id = uuid4()
    event_id = uuid4()
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id, next_turn_sequence=3)),
            FakeResult(rows=[]),
            FakeResult(
                rows=[
                    {"message_id": 10, "message_index": 0},
                    {"message_id": 11, "message_index": 1},
                ]
            ),
            FakeResult(),
            FakeResult(one=SimpleNamespace(event_id=event_id)),
        ]
    )

    result = await adapter(connection).append_turn(
        USER_ID,
        message(ConversationRole.USER, content="Câu hỏi"),
        message(ConversationRole.ASSISTANT, content="Trả lời"),
        schedule_memory=True,
    )

    assert result.inserted is True
    assert result.memory_job_event_id == event_id
    assert len(connection.calls) == 6
    job_statement, job_parameters = connection.calls[-1]
    assert "INSERT INTO memory_jobs" in str(job_statement)
    assert "ON CONFLICT (boundary_message_id) DO NOTHING" in str(job_statement)
    assert "content" not in str(job_statement)
    assert job_parameters is None


async def test_duplicate_scheduling_returns_existing_memory_event() -> None:
    conversation_id = uuid4()
    event_id = uuid4()
    existing = [
        {
            "conversation_id": conversation_id,
            "message_id": 10,
            "message_index": 0,
            "role": "user",
            "content": "Câu hỏi",
            "schema_version": 1,
        },
        {
            "conversation_id": conversation_id,
            "message_id": 11,
            "message_index": 1,
            "role": "assistant",
            "content": "Trả lời",
            "schema_version": 1,
        },
    ]
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id, next_turn_sequence=2)),
            FakeResult(rows=existing),
            FakeResult(one=SimpleNamespace(event_id=event_id)),
        ]
    )

    result = await adapter(connection).append_turn(
        USER_ID,
        message(ConversationRole.USER, content="Câu hỏi"),
        message(ConversationRole.ASSISTANT, content="Trả lời"),
        schedule_memory=True,
    )

    assert result.inserted is False
    assert result.memory_job_event_id == event_id
    assert "SELECT memory_jobs.event_id" in str(connection.calls[-1][0])


async def test_duplicate_turn_is_not_backfilled_when_scheduling_was_previously_disabled() -> None:
    conversation_id = uuid4()
    existing = [
        {
            "conversation_id": conversation_id,
            "message_id": 10,
            "message_index": 0,
            "role": "user",
            "content": "Câu hỏi",
            "schema_version": 1,
        },
        {
            "conversation_id": conversation_id,
            "message_id": 11,
            "message_index": 1,
            "role": "assistant",
            "content": "Trả lời",
            "schema_version": 1,
        },
    ]
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id, next_turn_sequence=2)),
            FakeResult(rows=existing),
            FakeResult(one=None),
        ]
    )

    result = await adapter(connection).append_turn(
        USER_ID,
        message(ConversationRole.USER, content="Câu hỏi"),
        message(ConversationRole.ASSISTANT, content="Trả lời"),
        schedule_memory=True,
    )

    assert result.inserted is False
    assert result.memory_job_event_id is None
    assert "SELECT memory_jobs.event_id" in str(connection.calls[-1][0])
    assert all("INSERT INTO memory_jobs" not in str(call[0]) for call in connection.calls)


async def test_append_turn_returns_false_for_matching_duplicate() -> None:
    conversation_id = uuid4()
    existing = [
        {
            "conversation_id": conversation_id,
            "message_id": 10,
            "message_index": 0,
            "role": "user",
            "content": "Câu hỏi",
            "schema_version": 1,
        },
        {
            "conversation_id": conversation_id,
            "message_id": 11,
            "message_index": 1,
            "role": "assistant",
            "content": "Trả lời",
            "schema_version": 1,
        },
    ]
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id, next_turn_sequence=2)),
            FakeResult(rows=existing),
        ]
    )

    result = await adapter(connection).append_turn(
        USER_ID,
        message(ConversationRole.USER, content="Câu hỏi"),
        message(ConversationRole.ASSISTANT, content="Trả lời"),
    )

    assert result.inserted is False
    assert result.reference.boundary_message_id == 11
    assert result.memory_job_event_id is None
    assert len(connection.calls) == 3


@pytest.mark.parametrize(
    "existing",
    [
        [{"conversation_id": uuid4(), "message_index": 0}],
        [
            {
                "conversation_id": uuid4(),
                "message_index": 0,
                "role": "user",
                "content": "other",
                "schema_version": 1,
            },
            {
                "conversation_id": uuid4(),
                "message_index": 1,
                "role": "assistant",
                "content": "assistant",
                "schema_version": 1,
            },
        ],
    ],
)
async def test_append_turn_rejects_partial_or_conflicting_duplicate(
    existing: list[dict[str, Any]],
) -> None:
    conversation_id = uuid4()
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult(one=SimpleNamespace(conversation_id=conversation_id, next_turn_sequence=2)),
            FakeResult(rows=existing),
        ]
    )

    with pytest.raises(ConversationStoreProtocolError):
        await adapter(connection).append_turn(
            USER_ID,
            message(ConversationRole.USER),
            message(ConversationRole.ASSISTANT),
        )


async def test_append_turn_rejects_missing_conversation_after_lock() -> None:
    connection = FakeConnection([FakeResult(), FakeResult(one=None)])

    with pytest.raises(ConversationStoreProtocolError):
        await adapter(connection).append_turn(
            USER_ID,
            message(ConversationRole.USER),
            message(ConversationRole.ASSISTANT),
        )


@pytest.mark.parametrize(
    "user,assistant",
    [
        (message(ConversationRole.ASSISTANT), message(ConversationRole.ASSISTANT)),
        (message(ConversationRole.USER), message(ConversationRole.USER)),
        (
            message(ConversationRole.USER, session_id="one"),
            message(ConversationRole.ASSISTANT, session_id="two"),
        ),
        (
            message(ConversationRole.USER, turn_id="one"),
            message(ConversationRole.ASSISTANT, turn_id="two"),
        ),
    ],
)
async def test_append_turn_validates_pair(
    user: ConversationMessage,
    assistant: ConversationMessage,
) -> None:
    with pytest.raises(ValueError):
        await adapter(FakeConnection()).append_turn(USER_ID, user, assistant)


async def test_append_turn_maps_database_integrity_error() -> None:
    error = IntegrityError("statement", {}, Exception("constraint"))

    with pytest.raises(ConversationStoreOperationError):
        await adapter(FakeConnection(error=error)).append_turn(
            USER_ID,
            message(ConversationRole.USER),
            message(ConversationRole.ASSISTANT),
        )


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
async def test_append_turn_requires_boolean_schedule_flag(value: object) -> None:
    with pytest.raises(ValueError, match="schedule_memory"):
        await adapter(FakeConnection()).append_turn(
            USER_ID,
            message(ConversationRole.USER),
            message(ConversationRole.ASSISTANT),
            schedule_memory=value,  # type: ignore[arg-type]
        )


async def test_scheduling_rejects_missing_or_malformed_event_reference() -> None:
    for row in (None, SimpleNamespace(event_id="not-a-uuid")):
        results = (
            [FakeResult(one=row), FakeResult(one=row)] if row is None else [FakeResult(one=row)]
        )
        with pytest.raises(ConversationStoreProtocolError):
            await PostgresConversationStoreAdapter._schedule_memory_job(
                FakeConnection(results),  # type: ignore[arg-type]
                42,
            )


class SqlStateError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("private database error")
        self.sqlstate = sqlstate


def test_sqlstate_helpers_detect_nested_configuration_and_connection_errors() -> None:
    root = SqlStateError("42P01")
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = root

    assert _find_sqlstate(wrapped) == "42P01"
    assert _is_connection("08006") is True
    assert _is_connection("57P03") is True
    assert _is_connection("23505") is False
    assert _is_connection(None) is False


def test_error_mapping_distinguishes_configuration_connection_and_operation() -> None:
    with pytest.raises(ConversationStoreConfigurationError):
        PostgresConversationStoreAdapter._raise_mapped(SqlStateError("42703"))
    with pytest.raises(ConversationStoreConfigurationError):
        PostgresConversationStoreAdapter._raise_mapped(SqlStateError("28P01"))

    connection_error = DBAPIError("statement", {}, SqlStateError("08006"), True)
    with pytest.raises(ConversationStoreConnectionError):
        PostgresConversationStoreAdapter._raise_mapped(connection_error)

    refused_error = DBAPIError("statement", {}, ConnectionRefusedError("private host"), False)
    with pytest.raises(ConversationStoreConnectionError):
        PostgresConversationStoreAdapter._raise_mapped(refused_error)

    with pytest.raises(ConversationStoreOperationError):
        PostgresConversationStoreAdapter._raise_mapped(RuntimeError("unexpected"))
