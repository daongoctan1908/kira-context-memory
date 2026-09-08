"""PostgreSQL implementation of durable conversation persistence."""

from builtins import TimeoutError as BuiltinTimeoutError
from collections.abc import Mapping
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.models.conversation import (
    AppendTurnResult,
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory_job import MEMORY_JOB_SCHEMA_VERSION
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    conversation_messages,
    conversations,
    memory_jobs,
)

_CONFIGURATION_SQLSTATES = {
    "28000",  # invalid authorization specification
    "28P01",  # invalid password
    "3D000",  # invalid catalog name
    "42501",  # insufficient privilege
    "42P01",  # undefined table
    "42703",  # undefined column (runtime schema mismatch)
}


class PostgresConversationStoreAdapter:
    """Persist full conversations and expose an indexed recent-message window."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def validate_schema(self) -> None:
        """Check connectivity and require the exact migration revision for this build."""
        try:
            async with self._engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

        if revision != EXPECTED_SCHEMA_REVISION:
            raise ConversationStoreConfigurationError

    async def read_recent(
        self,
        user_id: str,
        session_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Read the latest messages in chronological order without trimming history."""
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")

        recent_desc = (
            select(
                conversation_messages.c.turn_id,
                conversation_messages.c.role,
                conversation_messages.c.content,
                conversation_messages.c.message_timestamp,
                conversation_messages.c.schema_version,
                conversation_messages.c.turn_sequence,
                conversation_messages.c.message_index,
            )
            .select_from(
                conversation_messages.join(
                    conversations,
                    conversation_messages.c.conversation_id == conversations.c.conversation_id,
                )
            )
            .where(
                conversations.c.user_id == user_id,
                conversations.c.session_id == session_id,
            )
            .order_by(
                conversation_messages.c.turn_sequence.desc(),
                conversation_messages.c.message_index.desc(),
            )
            .limit(limit)
            .subquery("recent_messages")
        )
        chronological = select(recent_desc).order_by(
            recent_desc.c.turn_sequence.asc(),
            recent_desc.c.message_index.asc(),
        )

        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(chronological)).mappings().all()
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

        try:
            return tuple(self._to_domain_message(session_id, row) for row in rows)
        except (KeyError, TypeError, ValueError) as error:
            raise ConversationStoreProtocolError from error

    async def read_through_boundary(
        self,
        user_id: str,
        conversation_id: UUID,
        boundary_message_id: int,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Read a chronological window ending at an owned assistant message."""
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if (
            isinstance(boundary_message_id, bool)
            or not isinstance(boundary_message_id, int)
            or boundary_message_id < 1
        ):
            raise ValueError("boundary_message_id must be positive")
        if limit < 1:
            raise ValueError("limit must be positive")

        owner_query = (
            select(conversations.c.session_id)
            .select_from(
                conversations.join(
                    conversation_messages,
                    conversations.c.conversation_id == conversation_messages.c.conversation_id,
                )
            )
            .where(
                conversations.c.user_id == user_id,
                conversations.c.conversation_id == conversation_id,
                conversation_messages.c.message_id == boundary_message_id,
                conversation_messages.c.message_index == 1,
            )
        )
        recent_desc = (
            select(
                conversation_messages.c.turn_id,
                conversation_messages.c.role,
                conversation_messages.c.content,
                conversation_messages.c.message_timestamp,
                conversation_messages.c.schema_version,
                conversation_messages.c.turn_sequence,
                conversation_messages.c.message_index,
            )
            .where(
                conversation_messages.c.conversation_id == conversation_id,
                conversation_messages.c.message_id <= boundary_message_id,
            )
            .order_by(
                conversation_messages.c.turn_sequence.desc(),
                conversation_messages.c.message_index.desc(),
            )
            .limit(limit)
            .subquery("boundary_messages")
        )
        chronological = select(recent_desc).order_by(
            recent_desc.c.turn_sequence.asc(),
            recent_desc.c.message_index.asc(),
        )

        try:
            async with self._engine.connect() as connection:
                session_id = await connection.scalar(owner_query)
                if session_id is None:
                    raise ConversationStoreProtocolError
                rows = (await connection.execute(chronological)).mappings().all()
        except ConversationStoreProtocolError:
            raise
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

        try:
            return tuple(self._to_domain_message(session_id, row) for row in rows)
        except (KeyError, TypeError, ValueError) as error:
            raise ConversationStoreProtocolError from error

    async def append_turn(
        self,
        user_id: str,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        *,
        schedule_memory: bool = False,
    ) -> AppendTurnResult:
        """Atomically append a completed pair and its optional memory job."""
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not isinstance(schedule_memory, bool):
            raise ValueError("schedule_memory must be a boolean")
        self._validate_turn(user_message, assistant_message)

        try:
            async with self._engine.begin() as connection:
                conversation_id, turn_sequence = await self._lock_conversation(
                    connection,
                    user_id,
                    user_message.session_id,
                )
                existing = await self._read_existing_turn(connection, user_message.turn_id)
                if existing:
                    duplicate = self._validate_duplicate(
                        existing,
                        user_id,
                        conversation_id,
                        user_message,
                        assistant_message,
                    )
                    memory_job_event_id = None
                    if schedule_memory:
                        memory_job_event_id = await self._schedule_memory_job(
                            connection,
                            duplicate.reference.boundary_message_id,
                        )
                    return AppendTurnResult(
                        inserted=False,
                        reference=duplicate.reference,
                        memory_job_event_id=memory_job_event_id,
                    )

                inserted_rows = (
                    (
                        await connection.execute(
                            insert(conversation_messages).returning(
                                conversation_messages.c.message_id,
                                conversation_messages.c.message_index,
                            ),
                            [
                                self._message_values(
                                    conversation_id,
                                    turn_sequence,
                                    0,
                                    user_message,
                                ),
                                self._message_values(
                                    conversation_id,
                                    turn_sequence,
                                    1,
                                    assistant_message,
                                ),
                            ],
                        )
                    )
                    .mappings()
                    .all()
                )
                boundary_message_id = next(
                    (row["message_id"] for row in inserted_rows if row["message_index"] == 1),
                    None,
                )
                if not isinstance(boundary_message_id, int):
                    raise ConversationStoreProtocolError
                await connection.execute(
                    update(conversations)
                    .where(conversations.c.conversation_id == conversation_id)
                    .values(
                        next_turn_sequence=turn_sequence + 1,
                        updated_at=func.now(),
                    )
                )
                memory_job_event_id = None
                if schedule_memory:
                    memory_job_event_id = await self._schedule_memory_job(
                        connection,
                        boundary_message_id,
                    )
        except ConversationStoreProtocolError:
            raise
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

        return AppendTurnResult(
            inserted=True,
            reference=CompletedTurnReference(
                user_id=user_id,
                session_id=user_message.session_id,
                conversation_id=conversation_id,
                turn_id=user_message.turn_id,
                boundary_message_id=boundary_message_id,
            ),
            memory_job_event_id=memory_job_event_id,
        )

    @staticmethod
    async def _schedule_memory_job(
        connection: AsyncConnection,
        boundary_message_id: int,
    ) -> UUID:
        """Insert once per assistant boundary and return the stable event identifier."""
        candidate_event_id = uuid4()
        inserted = (
            await connection.execute(
                postgres_insert(memory_jobs)
                .values(
                    event_id=candidate_event_id,
                    boundary_message_id=boundary_message_id,
                    schema_version=MEMORY_JOB_SCHEMA_VERSION,
                )
                .on_conflict_do_nothing(index_elements=[memory_jobs.c.boundary_message_id])
                .returning(memory_jobs.c.event_id)
            )
        ).one_or_none()
        if inserted is not None:
            event_id = inserted.event_id
        else:
            existing = (
                await connection.execute(
                    select(memory_jobs.c.event_id).where(
                        memory_jobs.c.boundary_message_id == boundary_message_id
                    )
                )
            ).one_or_none()
            if existing is None:
                raise ConversationStoreProtocolError
            event_id = existing.event_id
        if not isinstance(event_id, UUID):
            raise ConversationStoreProtocolError
        return event_id

    async def _lock_conversation(
        self,
        connection: AsyncConnection,
        user_id: str,
        session_id: str,
    ) -> tuple[UUID, int]:
        candidate_id = uuid4()
        await connection.execute(
            postgres_insert(conversations)
            .values(
                conversation_id=candidate_id,
                user_id=user_id,
                session_id=session_id,
                next_turn_sequence=1,
            )
            .on_conflict_do_nothing(
                index_elements=[conversations.c.user_id, conversations.c.session_id]
            )
        )
        row = (
            await connection.execute(
                select(
                    conversations.c.conversation_id,
                    conversations.c.next_turn_sequence,
                )
                .where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ConversationStoreProtocolError
        return row.conversation_id, row.next_turn_sequence

    @staticmethod
    async def _read_existing_turn(
        connection: AsyncConnection,
        turn_id: str,
    ) -> list[Mapping[str, Any]]:
        result = await connection.execute(
            select(
                conversation_messages.c.conversation_id,
                conversation_messages.c.message_id,
                conversation_messages.c.message_index,
                conversation_messages.c.role,
                conversation_messages.c.content,
                conversation_messages.c.schema_version,
            )
            .where(conversation_messages.c.turn_id == turn_id)
            .order_by(conversation_messages.c.message_index)
        )
        return list(result.mappings().all())

    @staticmethod
    def _validate_duplicate(
        existing: list[Mapping[str, Any]],
        user_id: str,
        conversation_id: UUID,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
    ) -> AppendTurnResult:
        expected = (user_message, assistant_message)
        if len(existing) != 2:
            raise ConversationStoreProtocolError

        for index, (row, message) in enumerate(zip(existing, expected, strict=True)):
            if (
                row["conversation_id"] != conversation_id
                or row["message_index"] != index
                or row["role"] != message.role.value
                or row["content"] != message.content
                or row["schema_version"] != message.schema_version
            ):
                raise ConversationStoreProtocolError
        boundary_message_id = existing[1].get("message_id")
        if not isinstance(boundary_message_id, int):
            raise ConversationStoreProtocolError
        return AppendTurnResult(
            inserted=False,
            reference=CompletedTurnReference(
                user_id=user_id,
                session_id=user_message.session_id,
                conversation_id=conversation_id,
                turn_id=user_message.turn_id,
                boundary_message_id=boundary_message_id,
            ),
        )

    @staticmethod
    def _message_values(
        conversation_id: UUID,
        turn_sequence: int,
        message_index: int,
        message: ConversationMessage,
    ) -> dict[str, object]:
        return {
            "conversation_id": conversation_id,
            "turn_id": message.turn_id,
            "turn_sequence": turn_sequence,
            "message_index": message_index,
            "role": message.role.value,
            "content": message.content,
            "message_timestamp": message.timestamp,
            "schema_version": message.schema_version,
        }

    @staticmethod
    def _to_domain_message(
        session_id: str,
        row: Mapping[str, Any],
    ) -> ConversationMessage:
        timestamp = row["message_timestamp"]
        if not isinstance(timestamp, datetime):
            raise ValueError
        return ConversationMessage(
            session_id=session_id,
            turn_id=row["turn_id"],
            role=ConversationRole(row["role"]),
            content=row["content"],
            timestamp=timestamp,
            schema_version=row["schema_version"],
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
    def _raise_mapped(error: BaseException) -> NoReturn:
        sqlstate = _find_sqlstate(error)
        if sqlstate in _CONFIGURATION_SQLSTATES:
            raise ConversationStoreConfigurationError from error
        if _has_connection_failure(error):
            raise ConversationStoreConnectionError from error
        if isinstance(error, DBAPIError) and (
            error.connection_invalidated or _is_connection(sqlstate)
        ):
            raise ConversationStoreConnectionError from error
        if isinstance(error, IntegrityError):
            raise ConversationStoreOperationError from error
        if isinstance(error, SQLAlchemyError):
            raise ConversationStoreOperationError from error
        raise ConversationStoreOperationError from error


def _find_sqlstate(error: BaseException) -> str | None:
    for current in _error_chain(error):
        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if isinstance(sqlstate, str):
            return sqlstate
    return None


def _has_connection_failure(error: BaseException) -> bool:
    return any(
        isinstance(current, (BuiltinTimeoutError, OSError, SqlAlchemyTimeoutError))
        for current in _error_chain(error)
    )


def _error_chain(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    collected: list[BaseException] = []
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        collected.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, DBAPIError) and isinstance(current.orig, BaseException):
            pending.append(current.orig)
    return tuple(collected)


def _is_connection(sqlstate: str | None) -> bool:
    return sqlstate is not None and (sqlstate.startswith("08") or sqlstate == "57P03")
