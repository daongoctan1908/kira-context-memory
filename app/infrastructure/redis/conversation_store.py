"""Redis implementation of the recent-conversation store port."""

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from redis.asyncio import Redis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    RedisError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

from app.domain.errors.conversation import (
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.models.conversation import (
    CONVERSATION_MESSAGE_SCHEMA_VERSION,
    ConversationMessage,
    ConversationRole,
)

_APPEND_TURN_SCRIPT = """
local messages_key = KEYS[1]
local seen_key = KEYS[2]
local turn_id = ARGV[1]
local max_messages = tonumber(ARGV[4])
local ttl_seconds = tonumber(ARGV[5])
local redis_time = redis.call('TIME')
local now_seconds = tonumber(redis_time[1])

redis.call('ZREMRANGEBYSCORE', seen_key, '-inf', now_seconds - ttl_seconds)

if redis.call('ZSCORE', seen_key, turn_id) then
    redis.call('EXPIRE', messages_key, ttl_seconds)
    redis.call('EXPIRE', seen_key, ttl_seconds)
    return 0
end

redis.call('RPUSH', messages_key, ARGV[2], ARGV[3])
redis.call('LTRIM', messages_key, -max_messages, -1)
redis.call('ZADD', seen_key, now_seconds, turn_id)

redis.call('EXPIRE', messages_key, ttl_seconds)
redis.call('EXPIRE', seen_key, ttl_seconds)
return 1
"""


class RedisConversationStoreAdapter:
    """Keep a bounded, ordered and idempotent recent-message window in Redis."""

    def __init__(
        self,
        client: Redis,
        *,
        session_ttl_seconds: int,
        max_recent_messages: int,
        key_prefix: str = "kira:session",
    ) -> None:
        if session_ttl_seconds < 1:
            raise ValueError("session_ttl_seconds must be positive")
        if max_recent_messages < 2 or max_recent_messages % 2 != 0:
            raise ValueError("max_recent_messages must be a positive even number")
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be empty")

        self._client = client
        self._session_ttl_seconds = session_ttl_seconds
        self._max_recent_messages = max_recent_messages
        self._key_prefix = key_prefix.rstrip(":")

    async def ping(self) -> None:
        """Verify Redis connectivity without exposing connection details."""
        try:
            result = await self._client.ping()
        except (RedisConnectionError, RedisTimeoutError) as error:
            raise ConversationStoreConnectionError from error
        except RedisError as error:
            raise ConversationStoreOperationError from error
        if result is not True:
            raise ConversationStoreOperationError

    async def read_recent(
        self,
        session_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Read recent messages and refresh the inactivity TTL atomically."""
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")

        keys = self._session_keys(session_id)
        bounded_limit = min(limit, self._max_recent_messages)
        try:
            pipeline = self._client.pipeline(transaction=True)
            pipeline.lrange(keys.messages, -bounded_limit, -1)
            pipeline.expire(keys.messages, self._session_ttl_seconds)
            pipeline.expire(keys.seen_turns, self._session_ttl_seconds)
            results = await pipeline.execute()
        except (RedisConnectionError, RedisTimeoutError) as error:
            raise ConversationStoreConnectionError from error
        except RedisError as error:
            raise ConversationStoreOperationError from error

        if not isinstance(results, list) or not results:
            raise ConversationStoreProtocolError
        raw_messages = results[0]
        if not isinstance(raw_messages, list):
            raise ConversationStoreProtocolError

        messages = tuple(self._deserialize(item) for item in raw_messages)
        if any(message.session_id != session_id for message in messages):
            raise ConversationStoreProtocolError
        return messages

    async def append_turn(
        self,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
    ) -> bool:
        """Atomically append, deduplicate, trim and expire a completed turn."""
        self._validate_turn(user_message, assistant_message)
        keys = self._session_keys(user_message.session_id)
        try:
            result = await self._client.eval(
                _APPEND_TURN_SCRIPT,
                2,
                keys.messages,
                keys.seen_turns,
                user_message.turn_id,
                self._serialize(user_message),
                self._serialize(assistant_message),
                self._max_recent_messages,
                self._session_ttl_seconds,
            )
        except (RedisConnectionError, RedisTimeoutError) as error:
            raise ConversationStoreConnectionError from error
        except RedisError as error:
            raise ConversationStoreOperationError from error

        if result not in {0, 1}:
            raise ConversationStoreProtocolError
        return result == 1

    def _session_keys(self, session_id: str) -> "_SessionKeys":
        component = quote(session_id, safe="")
        hash_tag = f"{{{component}}}"
        base = f"{self._key_prefix}:{hash_tag}"
        return _SessionKeys(
            messages=f"{base}:messages",
            seen_turns=f"{base}:seen_turns",
        )

    @staticmethod
    def _validate_turn(
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
    ) -> None:
        if user_message.role is not ConversationRole.USER:
            raise ValueError("user_message must have the user role")
        if assistant_message.role is not ConversationRole.ASSISTANT:
            raise ValueError("assistant_message must have the assistant role")
        if user_message.session_id != assistant_message.session_id:
            raise ValueError("turn messages must have the same session_id")
        if user_message.turn_id != assistant_message.turn_id:
            raise ValueError("turn messages must have the same turn_id")

    @staticmethod
    def _serialize(message: ConversationMessage) -> str:
        timestamp = message.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return json.dumps(
            {
                "schema_version": message.schema_version,
                "session_id": message.session_id,
                "turn_id": message.turn_id,
                "role": message.role.value,
                "content": message.content,
                "timestamp": timestamp,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _deserialize(raw_message: Any) -> ConversationMessage:
        try:
            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode("utf-8")
            if not isinstance(raw_message, str):
                raise ValueError
            payload = json.loads(raw_message)
            if not isinstance(payload, dict):
                raise ValueError

            schema_version = payload.get("schema_version")
            if (
                isinstance(schema_version, bool)
                or not isinstance(schema_version, int)
                or schema_version != CONVERSATION_MESSAGE_SCHEMA_VERSION
            ):
                raise ValueError

            timestamp_value = payload.get("timestamp")
            if not isinstance(timestamp_value, str):
                raise ValueError
            if timestamp_value.endswith("Z"):
                timestamp_value = timestamp_value[:-1] + "+00:00"

            return ConversationMessage(
                schema_version=schema_version,
                session_id=payload["session_id"],
                turn_id=payload["turn_id"],
                role=ConversationRole(payload["role"]),
                content=payload["content"],
                timestamp=datetime.fromisoformat(timestamp_value),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConversationStoreProtocolError from error


class _SessionKeys:
    """Redis keys that share one cluster hash slot for a session."""

    __slots__ = ("messages", "seen_turns")

    def __init__(self, *, messages: str, seen_turns: str) -> None:
        self.messages = messages
        self.seen_turns = seen_turns
