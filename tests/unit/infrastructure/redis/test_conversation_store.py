import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from app.domain.errors.conversation import (
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.redis.conversation_store import RedisConversationStoreAdapter


class FakePipeline:
    def __init__(self, raw_messages: list[object] | None = None, error: Exception | None = None):
        self.raw_messages = raw_messages or []
        self.error = error
        self.commands: list[tuple[Any, ...]] = []

    def lrange(self, *args: object) -> "FakePipeline":
        self.commands.append(("lrange", *args))
        return self

    def expire(self, *args: object) -> "FakePipeline":
        self.commands.append(("expire", *args))
        return self

    async def execute(self) -> list[object]:
        if self.error is not None:
            raise self.error
        return [self.raw_messages, True, True]


class FakeRedis:
    def __init__(self) -> None:
        self.ping_result: object = True
        self.ping_error: Exception | None = None
        self.eval_result: object = 1
        self.eval_error: Exception | None = None
        self.eval_args: tuple[object, ...] = ()
        self.pipeline_instance = FakePipeline()

    async def ping(self) -> object:
        if self.ping_error is not None:
            raise self.ping_error
        return self.ping_result

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert transaction is True
        return self.pipeline_instance

    async def eval(self, *args: object) -> object:
        self.eval_args = args
        if self.eval_error is not None:
            raise self.eval_error
        return self.eval_result


def message(
    role: ConversationRole,
    *,
    session_id: str = "session: Hà Nội/1",
    turn_id: str = "turn-1",
    content: str = "Hưng Yên thì sao?",
    offset_hours: int = 7,
) -> ConversationMessage:
    return ConversationMessage(
        session_id=session_id,
        turn_id=turn_id,
        role=role,
        content=content,
        timestamp=datetime(2026, 9, 3, 8, tzinfo=timezone(timedelta(hours=offset_hours))),
    )


def adapter(client: FakeRedis, **changes: object) -> RedisConversationStoreAdapter:
    values = {"session_ttl_seconds": 86400, "max_recent_messages": 10}
    values.update(changes)
    return RedisConversationStoreAdapter(client, **values)  # type: ignore[arg-type]


async def test_append_serializes_unicode_normalizes_time_and_uses_cluster_safe_keys() -> None:
    client = FakeRedis()
    store = adapter(client)
    user = message(ConversationRole.USER)
    assistant = message(ConversationRole.ASSISTANT, content="Dạ, đúng rồi")

    assert await store.append_turn(user, assistant) is True

    _, key_count, messages_key, seen_key, turn_id, user_raw, assistant_raw, *_ = client.eval_args
    assert key_count == 2
    assert messages_key == "kira:session:{session%3A%20H%C3%A0%20N%E1%BB%99i%2F1}:messages"
    assert seen_key == "kira:session:{session%3A%20H%C3%A0%20N%E1%BB%99i%2F1}:seen_turns"
    assert turn_id == "turn-1"
    assert json.loads(user_raw)["content"] == "Hưng Yên thì sao?"  # type: ignore[arg-type]
    assert json.loads(user_raw)["timestamp"] == "2026-09-03T01:00:00Z"  # type: ignore[arg-type]
    assert json.loads(assistant_raw)["role"] == "assistant"  # type: ignore[arg-type]


async def test_duplicate_append_returns_false() -> None:
    client = FakeRedis()
    client.eval_result = 0

    inserted = await adapter(client).append_turn(
        message(ConversationRole.USER),
        message(ConversationRole.ASSISTANT),
    )

    assert inserted is False


async def test_read_recent_is_bounded_decodes_bytes_and_refreshes_all_ttls() -> None:
    client = FakeRedis()
    store = adapter(client)
    user = message(ConversationRole.USER)
    assistant = message(ConversationRole.ASSISTANT, content="answer")
    await store.append_turn(user, assistant)
    user_raw = client.eval_args[5]
    assistant_raw = client.eval_args[6]
    assert isinstance(user_raw, str)
    assert isinstance(assistant_raw, str)
    client.pipeline_instance = FakePipeline([user_raw.encode(), assistant_raw])

    result = await store.read_recent(user.session_id, limit=999)

    assert result == (user, assistant)
    assert client.pipeline_instance.commands[0][-2:] == (-10, -1)
    assert [command[0] for command in client.pipeline_instance.commands] == [
        "lrange",
        "expire",
        "expire",
    ]
    assert all(command[-1] == 86400 for command in client.pipeline_instance.commands[1:])


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        '{"schema_version":2}',
        '{"schema_version":1,"timestamp":"invalid"}',
        (
            '{"schema_version":1,"session_id":7,"turn_id":"turn-1","role":"user",'
            '"content":"question","timestamp":"2026-09-03T00:00:00Z"}'
        ),
    ],
)
async def test_read_rejects_malformed_or_unsupported_messages(raw: str) -> None:
    client = FakeRedis()
    client.pipeline_instance = FakePipeline([raw])

    with pytest.raises(ConversationStoreProtocolError):
        await adapter(client).read_recent("session-1", 10)


async def test_read_rejects_cross_session_payload() -> None:
    client = FakeRedis()
    store = adapter(client)
    await store.append_turn(
        message(ConversationRole.USER, session_id="other"),
        message(ConversationRole.ASSISTANT, session_id="other"),
    )
    client.pipeline_instance = FakePipeline([client.eval_args[5], client.eval_args[6]])

    with pytest.raises(ConversationStoreProtocolError):
        await store.read_recent("requested", 10)


@pytest.mark.parametrize(
    ("operation", "error", "expected"),
    [
        ("ping", RedisConnectionError("private host"), ConversationStoreConnectionError),
        ("ping", RedisError("private command"), ConversationStoreOperationError),
        ("read", RedisConnectionError("private host"), ConversationStoreConnectionError),
        ("read", RedisError("private command"), ConversationStoreOperationError),
        ("append", RedisConnectionError("private host"), ConversationStoreConnectionError),
        ("append", RedisError("private command"), ConversationStoreOperationError),
    ],
)
async def test_redis_errors_are_mapped_without_raw_details(
    operation: str,
    error: Exception,
    expected: type[Exception],
) -> None:
    client = FakeRedis()
    if operation == "ping":
        client.ping_error = error
    elif operation == "read":
        client.pipeline_instance = FakePipeline(error=error)
    else:
        client.eval_error = error
    store = adapter(client)

    with pytest.raises(expected) as caught:
        if operation == "ping":
            await store.ping()
        elif operation == "read":
            await store.read_recent("session-1", 10)
        else:
            await store.append_turn(
                message(ConversationRole.USER),
                message(ConversationRole.ASSISTANT),
            )

    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"session_ttl_seconds": 0},
        {"max_recent_messages": 1},
        {"max_recent_messages": 9},
        {"key_prefix": " "},
    ],
)
def test_adapter_rejects_invalid_configuration(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        adapter(FakeRedis(), **changes)


@pytest.mark.parametrize(
    ("user", "assistant"),
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
async def test_append_rejects_invalid_turn_pair(
    user: ConversationMessage,
    assistant: ConversationMessage,
) -> None:
    with pytest.raises(ValueError):
        await adapter(FakeRedis()).append_turn(user, assistant)


async def test_ping_and_unexpected_results_are_validated() -> None:
    client = FakeRedis()
    client.ping_result = False
    with pytest.raises(ConversationStoreOperationError):
        await adapter(client).ping()

    client.eval_result = "unexpected"
    with pytest.raises(ConversationStoreProtocolError):
        await adapter(client).append_turn(
            message(ConversationRole.USER),
            message(ConversationRole.ASSISTANT),
        )


def test_message_helper_uses_timezone_aware_timestamp() -> None:
    assert message(ConversationRole.USER, offset_hours=0).timestamp.tzinfo is UTC
