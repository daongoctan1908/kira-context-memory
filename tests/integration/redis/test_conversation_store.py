import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.domain.errors.conversation import ConversationStoreProtocolError
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.redis.conversation_store import RedisConversationStoreAdapter

pytestmark = pytest.mark.redis_integration


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    url = os.getenv("REDIS_TEST_URL")
    if not url:
        pytest.skip("REDIS_TEST_URL is not configured")

    client = Redis.from_url(url, decode_responses=True)
    await client.ping()
    yield client
    await client.aclose(close_connection_pool=True)


def message(
    session_id: str,
    turn_id: str,
    role: ConversationRole,
    content: str,
) -> ConversationMessage:
    return ConversationMessage(
        session_id=session_id,
        turn_id=turn_id,
        role=role,
        content=content,
        timestamp=datetime.now(UTC),
    )


async def make_store(
    client: Redis,
    *,
    ttl: int = 30,
    max_messages: int = 10,
) -> AsyncIterator[RedisConversationStoreAdapter]:
    prefix = f"test:kira:{uuid4().hex}"
    store = RedisConversationStoreAdapter(
        client,
        session_ttl_seconds=ttl,
        max_recent_messages=max_messages,
        key_prefix=prefix,
    )
    try:
        yield store
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)


async def append(
    store: RedisConversationStoreAdapter,
    session_id: str,
    turn_id: str,
) -> bool:
    return await store.append_turn(
        message(session_id, turn_id, ConversationRole.USER, f"Câu hỏi {turn_id}"),
        message(session_id, turn_id, ConversationRole.ASSISTANT, f"Trả lời {turn_id}"),
    )


async def test_real_redis_orders_trims_deduplicates_and_isolates_sessions(
    redis_client: Redis,
) -> None:
    async for store in make_store(redis_client, max_messages=4):
        assert await append(store, "session-a", "turn-1") is True
        assert await append(store, "session-a", "turn-2") is True
        assert await append(store, "session-a", "turn-3") is True
        assert await append(store, "session-a", "turn-1") is False
        assert await append(store, "session-b", "turn-b") is True

        recent_a = await store.read_recent("session-a", 10)
        recent_b = await store.read_recent("session-b", 10)

        assert [(item.turn_id, item.role) for item in recent_a] == [
            ("turn-2", ConversationRole.USER),
            ("turn-2", ConversationRole.ASSISTANT),
            ("turn-3", ConversationRole.USER),
            ("turn-3", ConversationRole.ASSISTANT),
        ]
        assert [item.content for item in recent_b] == ["Câu hỏi turn-b", "Trả lời turn-b"]


async def test_real_redis_refreshes_sliding_ttl_and_expires_session(redis_client: Redis) -> None:
    async for store in make_store(redis_client, ttl=2):
        await append(store, "ttl-session", "turn-1")
        await asyncio.sleep(1.1)

        assert len(await store.read_recent("ttl-session", 10)) == 2
        await asyncio.sleep(1.1)
        assert len(await store.read_recent("ttl-session", 10)) == 2
        await asyncio.sleep(2.1)

        assert await store.read_recent("ttl-session", 10) == ()


async def test_real_redis_keeps_each_concurrent_turn_pair_adjacent(redis_client: Redis) -> None:
    async for store in make_store(redis_client, max_messages=10):
        await asyncio.gather(*(append(store, "concurrent", f"turn-{index}") for index in range(8)))

        recent = await store.read_recent("concurrent", 10)

        assert len(recent) == 10
        for index in range(0, len(recent), 2):
            user, assistant = recent[index : index + 2]
            assert user.turn_id == assistant.turn_id
            assert user.role is ConversationRole.USER
            assert assistant.role is ConversationRole.ASSISTANT


async def test_real_redis_rejects_malformed_stored_message(redis_client: Redis) -> None:
    async for store in make_store(redis_client):
        keys = store._session_keys("malformed")
        await redis_client.rpush(keys.messages, "not-json")

        with pytest.raises(ConversationStoreProtocolError):
            await store.read_recent("malformed", 10)
