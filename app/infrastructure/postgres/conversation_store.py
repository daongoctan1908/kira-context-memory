"""PostgreSQL implementation of durable conversation persistence."""

import math
from builtins import TimeoutError as BuiltinTimeoutError
from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.errors.conversation import (
    ChatRequestConflictError,
    ChatRequestLeaseLostError,
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.models.conversation import (
    MAX_CONVERSATION_TITLE_LENGTH,
    AppendTurnResult,
    ChatRequestReservation,
    ChatRequestReservationOutcome,
    ChatRequestStatus,
    CompletedTurnReference,
    ConversationHistoryPage,
    ConversationListCursor,
    ConversationMessage,
    ConversationPage,
    ConversationRole,
    ConversationStatus,
    ConversationSummary,
)
from app.domain.models.memory_job import MEMORY_JOB_SCHEMA_VERSION
from app.domain.models.telemetry_context import (
    TelemetryContext,
    serialize_telemetry_context,
)
from app.infrastructure.postgres.schema import (
    CHAT_REQUEST_SCHEMA_REVISIONS,
    CONVERSATION_MANAGEMENT_SCHEMA_REVISIONS,
    SUPPORTED_SCHEMA_REVISIONS,
    TELEMETRY_CONTEXT_SCHEMA_REVISIONS,
    chat_requests,
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
        self._schema_revision: str | None = None

    async def validate_schema(self) -> None:
        """Check connectivity and require the exact migration revision for this build."""
        try:
            async with self._engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

        if revision not in SUPPORTED_SCHEMA_REVISIONS:
            raise ConversationStoreConfigurationError
        self._schema_revision = revision

    async def create_conversation(
        self,
        user_id: str,
        *,
        title: str | None = None,
    ) -> ConversationSummary:
        """Create one active conversation with an opaque public session identifier."""
        self._require_conversation_management_schema()
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        normalized_title = self._normalize_title(title)
        try:
            async with self._engine.begin() as connection:
                row = (
                    (
                        await connection.execute(
                            insert(conversations)
                            .values(
                                conversation_id=uuid4(),
                                user_id=user_id,
                                session_id=uuid4().hex,
                                title=normalized_title,
                                status=ConversationStatus.ACTIVE.value,
                                next_turn_sequence=1,
                            )
                            .returning(*self._conversation_summary_columns())
                        )
                    )
                    .mappings()
                    .one()
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        try:
            return self._to_conversation_summary(row)
        except (KeyError, TypeError, ValueError) as error:
            raise ConversationStoreProtocolError from error

    async def list_conversations(
        self,
        user_id: str,
        *,
        limit: int,
        cursor: ConversationListCursor | None = None,
    ) -> ConversationPage:
        """List owned conversations with stable descending keyset pagination."""
        self._require_conversation_management_schema()
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        self._validate_page_limit(limit)
        if cursor is not None and not isinstance(cursor, ConversationListCursor):
            raise ValueError("cursor must be a conversation list cursor")

        activity_at = func.coalesce(
            conversations.c.last_message_at,
            conversations.c.created_at,
        )
        statement = select(*self._conversation_summary_columns()).where(
            conversations.c.user_id == user_id
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    activity_at < cursor.activity_at,
                    (
                        (activity_at == cursor.activity_at)
                        & (conversations.c.conversation_id < cursor.conversation_id)
                    ),
                )
            )
        statement = statement.order_by(
            activity_at.desc(),
            conversations.c.conversation_id.desc(),
        ).limit(limit + 1)

        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).mappings().all()
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        try:
            items = tuple(self._to_conversation_summary(row) for row in rows[:limit])
        except (KeyError, TypeError, ValueError) as error:
            raise ConversationStoreProtocolError from error
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = ConversationListCursor(last.activity_at, last.conversation_id)
        return ConversationPage(items, next_cursor)

    async def read_history(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int,
        before_message_id: int | None = None,
    ) -> ConversationHistoryPage | None:
        """Read a chronological page from an active conversation owned by the caller."""
        self._require_conversation_management_schema()
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        self._validate_page_limit(limit)
        if before_message_id is not None and (
            isinstance(before_message_id, bool)
            or not isinstance(before_message_id, int)
            or before_message_id < 1
        ):
            raise ValueError("before_message_id must be positive")

        owner_query = select(conversations.c.conversation_id).where(
            conversations.c.user_id == user_id,
            conversations.c.session_id == session_id,
            conversations.c.status == ConversationStatus.ACTIVE.value,
        )
        statement = select(
            conversation_messages.c.message_id,
            conversation_messages.c.turn_id,
            conversation_messages.c.role,
            conversation_messages.c.content,
            conversation_messages.c.message_timestamp,
            conversation_messages.c.schema_version,
            conversation_messages.c.turn_sequence,
            conversation_messages.c.message_index,
        ).where(conversation_messages.c.conversation_id == owner_query.scalar_subquery())
        if before_message_id is not None:
            statement = statement.where(conversation_messages.c.message_id < before_message_id)
        statement = statement.order_by(
            conversation_messages.c.turn_sequence.desc(),
            conversation_messages.c.message_index.desc(),
        ).limit(limit + 1)

        try:
            async with self._engine.connect() as connection:
                conversation_id = await connection.scalar(owner_query)
                if conversation_id is None:
                    return None
                rows = (await connection.execute(statement)).mappings().all()
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

        visible_desc = rows[:limit]
        try:
            messages = tuple(
                self._to_domain_message(session_id, row) for row in reversed(visible_desc)
            )
            next_before_message_id = None
            if len(rows) > limit:
                next_before_message_id = visible_desc[-1]["message_id"]
                if not isinstance(next_before_message_id, int):
                    raise ValueError
            return ConversationHistoryPage(messages, next_before_message_id)
        except (KeyError, TypeError, ValueError) as error:
            raise ConversationStoreProtocolError from error

    async def mark_deletion_pending(self, user_id: str, session_id: str) -> bool:
        """Idempotently move an owned conversation into deletion-pending state."""
        self._require_conversation_management_schema()
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        try:
            async with self._engine.begin() as connection:
                row = (
                    await connection.execute(
                        select(conversations.c.conversation_id)
                        .where(
                            conversations.c.user_id == user_id,
                            conversations.c.session_id == session_id,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return False
                await connection.execute(
                    update(conversations)
                    .where(conversations.c.conversation_id == row.conversation_id)
                    .values(
                        status=ConversationStatus.DELETION_PENDING.value,
                        updated_at=func.now(),
                    )
                )
                return True
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def reserve_chat_request(
        self,
        user_id: str,
        session_id: str,
        client_message_id: UUID,
        content_hash: bytes,
        *,
        now: datetime,
        lease_seconds: float,
    ) -> ChatRequestReservation | None:
        """Reserve one fenced attempt while serializing requests per conversation."""
        self._require_chat_request_schema()
        self._validate_chat_reservation_input(
            user_id,
            session_id,
            client_message_id,
            content_hash,
            now,
            lease_seconds,
        )
        try:
            async with self._engine.begin() as connection:
                conversation = (
                    await connection.execute(
                        select(conversations.c.conversation_id)
                        .where(
                            conversations.c.user_id == user_id,
                            conversations.c.session_id == session_id,
                            conversations.c.status == ConversationStatus.ACTIVE.value,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if conversation is None:
                    return None

                existing = (
                    (
                        await connection.execute(
                            select(chat_requests)
                            .where(
                                chat_requests.c.conversation_id == conversation.conversation_id,
                                chat_requests.c.client_message_id == client_message_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    stored_hash = existing["content_hash"]
                    if not isinstance(stored_hash, (bytes, bytearray, memoryview)):
                        raise ConversationStoreProtocolError
                    if bytes(stored_hash) != content_hash:
                        raise ChatRequestConflictError
                    try:
                        status = ChatRequestStatus(existing["status"])
                    except (TypeError, ValueError) as error:
                        raise ConversationStoreProtocolError from error
                    if status is ChatRequestStatus.COMPLETED:
                        return self._to_chat_reservation(
                            existing,
                            ChatRequestReservationOutcome.COMPLETED,
                            expose_lease=False,
                        )
                    if status is ChatRequestStatus.PROCESSING:
                        expires_at = existing["lease_expires_at"]
                        if not isinstance(expires_at, datetime):
                            raise ConversationStoreProtocolError
                        if expires_at > now:
                            return self._to_chat_reservation(
                                existing,
                                ChatRequestReservationOutcome.IN_PROGRESS,
                                expose_lease=False,
                            )
                    return await self._reclaim_chat_request(
                        connection,
                        existing,
                        now=now,
                        lease_seconds=lease_seconds,
                    )

                processing = (
                    (
                        await connection.execute(
                            select(chat_requests).where(
                                chat_requests.c.conversation_id == conversation.conversation_id,
                                chat_requests.c.status == ChatRequestStatus.PROCESSING.value,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if processing is not None:
                    return self._to_chat_reservation(
                        processing,
                        ChatRequestReservationOutcome.IN_PROGRESS,
                        expose_lease=False,
                    )

                request_id = uuid4()
                lease_token = uuid4()
                lease_expires_at = now + timedelta(seconds=lease_seconds)
                inserted = (
                    (
                        await connection.execute(
                            insert(chat_requests)
                            .values(
                                request_id=request_id,
                                conversation_id=conversation.conversation_id,
                                client_message_id=client_message_id,
                                content_hash=content_hash,
                                turn_id=uuid4().hex,
                                status=ChatRequestStatus.PROCESSING.value,
                                attempt_count=1,
                                lease_token=lease_token,
                                lease_expires_at=lease_expires_at,
                                created_at=now,
                                updated_at=now,
                            )
                            .returning(*chat_requests.c)
                        )
                    )
                    .mappings()
                    .one()
                )
                return self._to_chat_reservation(
                    inserted,
                    ChatRequestReservationOutcome.ACQUIRED,
                    expose_lease=True,
                )
        except (ChatRequestConflictError, ConversationStoreProtocolError):
            raise
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def abandon_chat_request(
        self,
        request_id: UUID,
        lease_token: UUID,
        *,
        status: ChatRequestStatus,
        now: datetime,
    ) -> bool:
        """Fence failure/cancellation transitions by the current attempt token."""
        self._require_chat_request_schema()
        if not isinstance(request_id, UUID) or not isinstance(lease_token, UUID):
            raise ValueError("request_id and lease_token must be UUIDs")
        if status not in {ChatRequestStatus.FAILED, ChatRequestStatus.CANCELLED}:
            raise ValueError("abandoned chat request status must be failed or cancelled")
        self._require_aware_datetime(now, "now")
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(
                    update(chat_requests)
                    .where(
                        chat_requests.c.request_id == request_id,
                        chat_requests.c.status == ChatRequestStatus.PROCESSING.value,
                        chat_requests.c.lease_token == lease_token,
                    )
                    .values(
                        status=status.value,
                        lease_token=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )
                return bool(result.rowcount)
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def complete_chat_request(
        self,
        user_id: str,
        reservation: ChatRequestReservation,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        *,
        completed_at: datetime,
        schedule_memory: bool = False,
        telemetry_context: TelemetryContext | None = None,
    ) -> AppendTurnResult:
        """Commit the turn, optional job and request state under one fenced lease."""
        self._require_chat_request_schema()
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not isinstance(reservation, ChatRequestReservation):
            raise ValueError("reservation must be a chat request reservation")
        if (
            reservation.outcome
            not in {
                ChatRequestReservationOutcome.ACQUIRED,
                ChatRequestReservationOutcome.RECLAIMED,
            }
            or reservation.lease_token is None
        ):
            raise ValueError("completion requires an owned chat request reservation")
        if not isinstance(schedule_memory, bool):
            raise ValueError("schedule_memory must be a boolean")
        self._require_aware_datetime(completed_at, "completed_at")
        self._validate_turn(user_message, assistant_message)
        if user_message.turn_id != reservation.turn_id:
            raise ValueError("turn messages must match the reserved turn_id")

        try:
            async with self._engine.begin() as connection:
                conversation = (
                    await connection.execute(
                        select(
                            conversations.c.conversation_id,
                            conversations.c.session_id,
                            conversations.c.next_turn_sequence,
                        )
                        .where(
                            conversations.c.conversation_id == reservation.conversation_id,
                            conversations.c.user_id == user_id,
                            conversations.c.status == ConversationStatus.ACTIVE.value,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if conversation is None:
                    raise ChatRequestLeaseLostError
                if user_message.session_id != conversation.session_id:
                    raise ValueError("turn messages must match the reserved conversation")

                request_row = (
                    (
                        await connection.execute(
                            select(chat_requests)
                            .where(
                                chat_requests.c.request_id == reservation.request_id,
                                chat_requests.c.conversation_id == reservation.conversation_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if not self._owns_chat_request_attempt(request_row, reservation):
                    raise ChatRequestLeaseLostError
                stored_hash = request_row["content_hash"]
                if (
                    not isinstance(stored_hash, (bytes, bytearray, memoryview))
                    or bytes(stored_hash) != sha256(user_message.content.encode("utf-8")).digest()
                ):
                    raise ChatRequestConflictError

                completed = await connection.execute(
                    update(chat_requests)
                    .where(
                        chat_requests.c.request_id == reservation.request_id,
                        chat_requests.c.status == ChatRequestStatus.PROCESSING.value,
                        chat_requests.c.lease_token == reservation.lease_token,
                        chat_requests.c.lease_expires_at > func.now(),
                    )
                    .values(
                        status=ChatRequestStatus.COMPLETED.value,
                        lease_token=None,
                        lease_expires_at=None,
                        updated_at=completed_at,
                        completed_at=completed_at,
                    )
                )
                if completed.rowcount != 1:
                    raise ChatRequestLeaseLostError

                turn_sequence = conversation.next_turn_sequence
                if isinstance(turn_sequence, bool) or not isinstance(turn_sequence, int):
                    raise ConversationStoreProtocolError
                inserted_rows = (
                    (
                        await connection.execute(
                            insert(conversation_messages).returning(
                                conversation_messages.c.message_id,
                                conversation_messages.c.message_index,
                            ),
                            [
                                self._message_values(
                                    reservation.conversation_id,
                                    turn_sequence,
                                    0,
                                    user_message,
                                ),
                                self._message_values(
                                    reservation.conversation_id,
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

                memory_job_event_id = None
                if schedule_memory:
                    memory_job_event_id = await self._schedule_memory_job(
                        connection,
                        boundary_message_id,
                        telemetry_context,
                        supports_telemetry_context=(
                            self._schema_revision in TELEMETRY_CONTEXT_SCHEMA_REVISIONS
                        ),
                    )

                await connection.execute(
                    update(conversations)
                    .where(conversations.c.conversation_id == reservation.conversation_id)
                    .values(
                        next_turn_sequence=turn_sequence + 1,
                        updated_at=completed_at,
                        last_message_at=assistant_message.timestamp,
                    )
                )
        except (
            ChatRequestConflictError,
            ChatRequestLeaseLostError,
            ConversationStoreProtocolError,
        ):
            raise
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

        return AppendTurnResult(
            inserted=True,
            reference=CompletedTurnReference(
                user_id=user_id,
                session_id=user_message.session_id,
                conversation_id=reservation.conversation_id,
                turn_id=reservation.turn_id,
                boundary_message_id=boundary_message_id,
            ),
            memory_job_event_id=memory_job_event_id,
        )

    async def read_completed_chat_request(
        self,
        user_id: str,
        session_id: str,
        reservation: ChatRequestReservation,
    ) -> tuple[ConversationMessage, ConversationMessage] | None:
        """Read a completed pair through relational ownership and request state."""
        self._require_chat_request_schema()
        if not user_id.strip() or not session_id.strip():
            raise ValueError("user_id and session_id must not be empty")
        if (
            not isinstance(reservation, ChatRequestReservation)
            or reservation.outcome is not ChatRequestReservationOutcome.COMPLETED
        ):
            raise ValueError("replay requires a completed chat request reservation")
        statement = (
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
                chat_requests.join(
                    conversations,
                    chat_requests.c.conversation_id == conversations.c.conversation_id,
                ).join(
                    conversation_messages,
                    (conversation_messages.c.conversation_id == conversations.c.conversation_id)
                    & (conversation_messages.c.turn_id == chat_requests.c.turn_id),
                )
            )
            .where(
                chat_requests.c.request_id == reservation.request_id,
                chat_requests.c.status == ChatRequestStatus.COMPLETED.value,
                conversations.c.conversation_id == reservation.conversation_id,
                conversations.c.user_id == user_id,
                conversations.c.session_id == session_id,
                conversations.c.status == ConversationStatus.ACTIVE.value,
            )
            .order_by(conversation_messages.c.message_index)
        )
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).mappings().all()
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        if not rows:
            return None
        if len(rows) != 2:
            raise ConversationStoreProtocolError
        try:
            messages = tuple(self._to_domain_message(session_id, row) for row in rows)
        except (KeyError, TypeError, ValueError) as error:
            raise ConversationStoreProtocolError from error
        if (
            messages[0].role is not ConversationRole.USER
            or messages[1].role is not ConversationRole.ASSISTANT
            or any(message.turn_id != reservation.turn_id for message in messages)
        ):
            raise ConversationStoreProtocolError
        return messages

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

        filters = [
            conversations.c.user_id == user_id,
            conversations.c.session_id == session_id,
        ]
        if self._supports_conversation_management:
            filters.append(conversations.c.status == ConversationStatus.ACTIVE.value)
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
            .where(*filters)
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
        telemetry_context: TelemetryContext | None = None,
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
                        memory_job_event_id = await self._read_memory_job_event_id(
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
                conversation_values: dict[str, object] = {
                    "next_turn_sequence": turn_sequence + 1,
                    "updated_at": func.now(),
                }
                if self._supports_conversation_management:
                    conversation_values["last_message_at"] = assistant_message.timestamp
                await connection.execute(
                    update(conversations)
                    .where(conversations.c.conversation_id == conversation_id)
                    .values(**conversation_values)
                )
                memory_job_event_id = None
                if schedule_memory:
                    memory_job_event_id = await self._schedule_memory_job(
                        connection,
                        boundary_message_id,
                        telemetry_context,
                        supports_telemetry_context=(
                            self._schema_revision in TELEMETRY_CONTEXT_SCHEMA_REVISIONS
                        ),
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
    async def _read_memory_job_event_id(
        connection: AsyncConnection,
        boundary_message_id: int,
    ) -> UUID | None:
        """Return an existing job for a duplicate turn without backfilling old work."""
        existing = (
            await connection.execute(
                select(memory_jobs.c.event_id).where(
                    memory_jobs.c.boundary_message_id == boundary_message_id
                )
            )
        ).one_or_none()
        if existing is None:
            return None
        event_id = existing.event_id
        if not isinstance(event_id, UUID):
            raise ConversationStoreProtocolError
        return event_id

    @staticmethod
    async def _schedule_memory_job(
        connection: AsyncConnection,
        boundary_message_id: int,
        telemetry_context: TelemetryContext | None = None,
        *,
        supports_telemetry_context: bool = False,
    ) -> UUID:
        """Insert once per assistant boundary and return the stable event identifier."""
        candidate_event_id = uuid4()
        values: dict[str, object] = {
            "event_id": candidate_event_id,
            "boundary_message_id": boundary_message_id,
            "schema_version": MEMORY_JOB_SCHEMA_VERSION,
        }
        if supports_telemetry_context:
            values["telemetry_context"] = serialize_telemetry_context(telemetry_context)
        inserted = (
            await connection.execute(
                postgres_insert(memory_jobs)
                .values(**values)
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
        filters = [
            conversations.c.user_id == user_id,
            conversations.c.session_id == session_id,
        ]
        if self._supports_conversation_management:
            filters.append(conversations.c.status == ConversationStatus.ACTIVE.value)
        row = (
            await connection.execute(
                select(
                    conversations.c.conversation_id,
                    conversations.c.next_turn_sequence,
                )
                .where(*filters)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ConversationStoreProtocolError
        return row.conversation_id, row.next_turn_sequence

    @property
    def _supports_conversation_management(self) -> bool:
        return self._schema_revision in CONVERSATION_MANAGEMENT_SCHEMA_REVISIONS

    def _require_conversation_management_schema(self) -> None:
        if not self._supports_conversation_management:
            raise ConversationStoreConfigurationError

    def _require_chat_request_schema(self) -> None:
        if self._schema_revision not in CHAT_REQUEST_SCHEMA_REVISIONS:
            raise ConversationStoreConfigurationError

    @staticmethod
    async def _reclaim_chat_request(
        connection: AsyncConnection,
        existing: Mapping[str, Any],
        *,
        now: datetime,
        lease_seconds: float,
    ) -> ChatRequestReservation:
        attempt_count = existing.get("attempt_count")
        request_id = existing.get("request_id")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 1
            or not isinstance(request_id, UUID)
        ):
            raise ConversationStoreProtocolError
        lease_token = uuid4()
        reclaimed = (
            (
                await connection.execute(
                    update(chat_requests)
                    .where(chat_requests.c.request_id == request_id)
                    .values(
                        status=ChatRequestStatus.PROCESSING.value,
                        attempt_count=attempt_count + 1,
                        lease_token=lease_token,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                        completed_at=None,
                    )
                    .returning(*chat_requests.c)
                )
            )
            .mappings()
            .one()
        )
        return PostgresConversationStoreAdapter._to_chat_reservation(
            reclaimed,
            ChatRequestReservationOutcome.RECLAIMED,
            expose_lease=True,
        )

    @staticmethod
    def _to_chat_reservation(
        row: Mapping[str, Any],
        outcome: ChatRequestReservationOutcome,
        *,
        expose_lease: bool,
    ) -> ChatRequestReservation:
        try:
            return ChatRequestReservation(
                request_id=row["request_id"],
                conversation_id=row["conversation_id"],
                client_message_id=row["client_message_id"],
                turn_id=row["turn_id"],
                status=ChatRequestStatus(row["status"]),
                outcome=outcome,
                attempt_count=row["attempt_count"],
                lease_token=row["lease_token"] if expose_lease else None,
                lease_expires_at=row["lease_expires_at"] if expose_lease else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ConversationStoreProtocolError from error

    @staticmethod
    def _owns_chat_request_attempt(
        row: Mapping[str, Any] | None,
        reservation: ChatRequestReservation,
    ) -> bool:
        if row is None:
            return False
        try:
            return bool(
                row["status"] == ChatRequestStatus.PROCESSING.value
                and row["turn_id"] == reservation.turn_id
                and row["client_message_id"] == reservation.client_message_id
                and row["lease_token"] == reservation.lease_token
                and isinstance(row["lease_expires_at"], datetime)
            )
        except (KeyError, TypeError):
            raise ConversationStoreProtocolError from None

    @staticmethod
    def _validate_chat_reservation_input(
        user_id: str,
        session_id: str,
        client_message_id: UUID,
        content_hash: bytes,
        now: datetime,
        lease_seconds: float,
    ) -> None:
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if not isinstance(client_message_id, UUID):
            raise ValueError("client_message_id must be a UUID")
        if not isinstance(content_hash, bytes) or len(content_hash) != 32:
            raise ValueError("content_hash must be 32 bytes")
        PostgresConversationStoreAdapter._require_aware_datetime(now, "now")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or not 10 <= float(lease_seconds) <= 900
        ):
            raise ValueError("lease_seconds must be between 10 and 900")

    @staticmethod
    def _require_aware_datetime(value: datetime, name: str) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _conversation_summary_columns() -> tuple[Any, ...]:
        return (
            conversations.c.conversation_id,
            conversations.c.session_id,
            conversations.c.title,
            conversations.c.status,
            conversations.c.created_at,
            conversations.c.updated_at,
            conversations.c.last_message_at,
        )

    @staticmethod
    def _to_conversation_summary(row: Mapping[str, Any]) -> ConversationSummary:
        return ConversationSummary(
            conversation_id=row["conversation_id"],
            session_id=row["session_id"],
            title=row["title"],
            status=ConversationStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_message_at=row["last_message_at"],
        )

    @staticmethod
    def _normalize_title(title: str | None) -> str | None:
        if title is None:
            return None
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        normalized = title.strip()
        if not normalized or len(normalized) > MAX_CONVERSATION_TITLE_LENGTH:
            raise ValueError("title must be between 1 and 200 characters")
        return normalized

    @staticmethod
    def _validate_page_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("page limit must be between 1 and 100")

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
