from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.postgres.conversation_store import (
    PostgresConversationStoreAdapter,
    _find_sqlstate,
    _is_connection,
)
from app.infrastructure.postgres.schema import EXPECTED_SCHEMA_REVISION

USER_ID = "user-1"


class FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        one: object | None = None,
    ) -> None:
        self._rows = rows or []
        self._one = one

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


async def test_validate_schema_rejects_wrong_revision() -> None:
    with pytest.raises(ConversationStoreConfigurationError):
        await adapter(FakeConnection(scalar="old-revision")).validate_schema()


async def test_validate_schema_maps_connection_timeout() -> None:
    with pytest.raises(ConversationStoreConnectionError):
        await adapter(
            FakeConnection(), enter_error=SqlAlchemyTimeoutError("unavailable")
        ).validate_schema()


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
    with pytest.raises(ConversationStoreProtocolError):
        await adapter(FakeConnection(scalar=None)).read_through_boundary(USER_ID, uuid4(), 42, 10)


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
    assert result.reference.user_id == USER_ID
    assert len(connection.calls) == 5
    pair = connection.calls[3][1]
    assert isinstance(pair, list)
    assert [item["message_index"] for item in pair] == [0, 1]
    assert [item["turn_sequence"] for item in pair] == [3, 3]
    assert [item["content"] for item in pair] == ["Câu hỏi", "Trả lời"]


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
