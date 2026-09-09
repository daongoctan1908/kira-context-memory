"""PostgreSQL leased queue implementation for asynchronous memory formation."""

import math
import re
from builtins import TimeoutError as BuiltinTimeoutError
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.errors.memory_job import (
    MemoryJobLeaseLostError,
    MemoryJobQueueConfigurationError,
    MemoryJobQueueConnectionError,
    MemoryJobQueueOperationError,
    MemoryJobQueueProtocolError,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory_job import (
    DeadMemoryJob,
    MemoryJob,
    MemoryJobPurgeResult,
    MemoryJobStats,
    MemoryJobStatus,
)
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    conversation_messages,
    conversations,
    memory_jobs,
)

_CONFIGURATION_SQLSTATES = {
    "28000",
    "28P01",
    "3D000",
    "42501",
    "42P01",
    "42703",
}
_ERROR_CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_SMALLINT = 32_767
_ATTEMPTS_EXHAUSTED_ERROR_CLASS = "MemoryJobAttemptsExhaustedError"


class PostgresMemoryJobQueueAdapter:
    """Claim and transition reference-only memory jobs with database-backed leases."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def validate_schema(self) -> None:
        """Check connectivity and require the exact application schema revision."""
        try:
            async with self._engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        if revision != EXPECTED_SCHEMA_REVISION:
            raise MemoryJobQueueConfigurationError

    async def claim_due(
        self,
        *,
        lease_owner: UUID,
        limit: int,
        lease_seconds: float,
        max_attempts: int,
    ) -> tuple[MemoryJob, ...]:
        """Lease due pending or expired processing rows without blocking peer workers."""
        _require_uuid(lease_owner, "lease_owner")
        _require_positive_integer(limit, "limit")
        _require_positive_integer(max_attempts, "max_attempts", maximum=_MAX_SMALLINT)
        _require_positive_finite_number(lease_seconds, "lease_seconds")

        try:
            async with self._engine.begin() as connection:
                rows = (
                    (await connection.execute(self._claim_candidates(limit, max_attempts)))
                    .mappings()
                    .all()
                )
                claimed = [
                    await self._lease_candidate(
                        connection,
                        row,
                        lease_owner,
                        float(lease_seconds),
                    )
                    for row in rows
                ]
        except MemoryJobQueueProtocolError:
            raise
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        return tuple(claimed)

    async def complete(
        self,
        event_id: UUID,
        lease_token: UUID,
        *,
        lifecycle_event_count: int,
    ) -> None:
        """Record successful formation while the current lease is still valid."""
        _require_uuid(event_id, "event_id")
        _require_uuid(lease_token, "lease_token")
        _require_nonnegative_integer(lifecycle_event_count, "lifecycle_event_count")
        await self._leased_transition(
            event_id,
            lease_token,
            {
                "status": MemoryJobStatus.COMPLETED.value,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_class": None,
                "lifecycle_event_count": lifecycle_event_count,
                "completed_at": func.now(),
                "dead_at": None,
                "updated_at": func.now(),
            },
        )

    async def retry(
        self,
        event_id: UUID,
        lease_token: UUID,
        *,
        next_attempt_at: datetime,
        error_class: str,
    ) -> None:
        """Release a valid lease back to the due-time ordered pending queue."""
        _require_uuid(event_id, "event_id")
        _require_uuid(lease_token, "lease_token")
        _require_aware_datetime(next_attempt_at, "next_attempt_at")
        _require_error_class(error_class)
        await self._leased_transition(
            event_id,
            lease_token,
            {
                "status": MemoryJobStatus.PENDING.value,
                "next_attempt_at": next_attempt_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_class": error_class,
                "lifecycle_event_count": None,
                "completed_at": None,
                "dead_at": None,
                "updated_at": func.now(),
            },
        )

    async def dead_letter(
        self,
        event_id: UUID,
        lease_token: UUID,
        *,
        error_class: str,
    ) -> None:
        """Move a valid lease to the terminal dead state."""
        _require_uuid(event_id, "event_id")
        _require_uuid(lease_token, "lease_token")
        _require_error_class(error_class)
        await self._leased_transition(
            event_id,
            lease_token,
            {
                "status": MemoryJobStatus.DEAD.value,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_class": error_class,
                "lifecycle_event_count": None,
                "completed_at": None,
                "dead_at": func.now(),
                "updated_at": func.now(),
            },
        )

    async def stats(self) -> MemoryJobStats:
        """Return global low-cardinality counts and oldest pending age."""
        statement = select(
            func.count()
            .filter(memory_jobs.c.status == MemoryJobStatus.PENDING.value)
            .label("pending"),
            func.count()
            .filter(memory_jobs.c.status == MemoryJobStatus.PROCESSING.value)
            .label("processing"),
            func.count()
            .filter(memory_jobs.c.status == MemoryJobStatus.COMPLETED.value)
            .label("completed"),
            func.count().filter(memory_jobs.c.status == MemoryJobStatus.DEAD.value).label("dead"),
            func.extract(
                "epoch",
                func.now()
                - func.min(memory_jobs.c.created_at).filter(
                    memory_jobs.c.status == MemoryJobStatus.PENDING.value
                ),
            ).label("oldest_pending_age_seconds"),
        )
        try:
            async with self._engine.connect() as connection:
                row = (await connection.execute(statement)).mappings().one()
            age = row["oldest_pending_age_seconds"]
            return MemoryJobStats(
                pending=row["pending"],
                processing=row["processing"],
                completed=row["completed"],
                dead=row["dead"],
                oldest_pending_age_seconds=None if age is None else max(float(age), 0.0),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MemoryJobQueueProtocolError from error
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def list_dead(self, *, limit: int) -> tuple[DeadMemoryJob, ...]:
        """Return newest dead jobs without boundary or conversation identifiers."""
        _require_positive_integer(limit, "limit")
        statement = (
            select(
                memory_jobs.c.event_id,
                memory_jobs.c.attempt_count,
                memory_jobs.c.requeue_count,
                memory_jobs.c.last_error_class,
                memory_jobs.c.created_at,
                memory_jobs.c.dead_at,
            )
            .where(memory_jobs.c.status == MemoryJobStatus.DEAD.value)
            .order_by(memory_jobs.c.dead_at.desc(), memory_jobs.c.event_id.asc())
            .limit(limit)
        )
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).mappings().all()
            return tuple(self._to_dead_job(row) for row in rows)
        except (KeyError, TypeError, ValueError) as error:
            raise MemoryJobQueueProtocolError from error
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def requeue_dead(self, event_id: UUID) -> bool:
        """Reset one explicit dead job so it can be processed from attempt one."""
        _require_uuid(event_id, "event_id")
        statement = (
            update(memory_jobs)
            .where(
                memory_jobs.c.event_id == event_id,
                memory_jobs.c.status == MemoryJobStatus.DEAD.value,
            )
            .values(
                status=MemoryJobStatus.PENDING.value,
                attempt_count=0,
                requeue_count=memory_jobs.c.requeue_count + 1,
                next_attempt_at=func.now(),
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error_class=None,
                lifecycle_event_count=None,
                completed_at=None,
                dead_at=None,
                updated_at=func.now(),
            )
        )
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(statement)
                return result.rowcount == 1
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def purge_terminal(
        self,
        *,
        completed_before: datetime,
        dead_before: datetime,
        limit: int,
    ) -> MemoryJobPurgeResult:
        """Delete one deterministic bounded batch of expired terminal rows."""
        _require_aware_datetime(completed_before, "completed_before")
        _require_aware_datetime(dead_before, "dead_before")
        _require_positive_integer(limit, "limit")
        terminal_at = case(
            (
                memory_jobs.c.status == MemoryJobStatus.COMPLETED.value,
                memory_jobs.c.completed_at,
            ),
            else_=memory_jobs.c.dead_at,
        )
        candidates = (
            select(memory_jobs.c.event_id)
            .where(
                or_(
                    (
                        (memory_jobs.c.status == MemoryJobStatus.COMPLETED.value)
                        & (memory_jobs.c.completed_at < completed_before)
                    ),
                    (
                        (memory_jobs.c.status == MemoryJobStatus.DEAD.value)
                        & (memory_jobs.c.dead_at < dead_before)
                    ),
                )
            )
            .order_by(terminal_at.asc(), memory_jobs.c.event_id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True, of=memory_jobs)
            .cte("expired_memory_jobs")
        )
        statement = (
            delete(memory_jobs)
            .where(memory_jobs.c.event_id.in_(select(candidates.c.event_id)))
            .returning(memory_jobs.c.status)
        )
        try:
            async with self._engine.begin() as connection:
                statuses = (await connection.execute(statement)).scalars().all()
            unknown = set(statuses) - {
                MemoryJobStatus.COMPLETED.value,
                MemoryJobStatus.DEAD.value,
            }
            if unknown:
                raise MemoryJobQueueProtocolError
            return MemoryJobPurgeResult(
                completed=statuses.count(MemoryJobStatus.COMPLETED.value),
                dead=statuses.count(MemoryJobStatus.DEAD.value),
            )
        except MemoryJobQueueProtocolError:
            raise
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    @staticmethod
    def _claim_candidates(limit: int, max_attempts: int):
        exhausted_ids = (
            select(memory_jobs.c.event_id)
            .where(
                memory_jobs.c.status == MemoryJobStatus.PROCESSING.value,
                memory_jobs.c.lease_expires_at <= func.now(),
                memory_jobs.c.attempt_count >= max_attempts,
            )
            .order_by(memory_jobs.c.lease_expires_at.asc(), memory_jobs.c.event_id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True, of=memory_jobs)
            .cte("exhausted_memory_job_ids")
        )
        dead_exhausted = (
            update(memory_jobs)
            .where(
                memory_jobs.c.event_id.in_(select(exhausted_ids.c.event_id)),
                memory_jobs.c.status == MemoryJobStatus.PROCESSING.value,
                memory_jobs.c.lease_expires_at <= func.now(),
                memory_jobs.c.attempt_count >= max_attempts,
            )
            .values(
                status=MemoryJobStatus.DEAD.value,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error_class=_ATTEMPTS_EXHAUSTED_ERROR_CLASS,
                lifecycle_event_count=None,
                completed_at=None,
                dead_at=func.now(),
                updated_at=func.now(),
            )
            .returning(memory_jobs.c.event_id)
            .cte("dead_exhausted_memory_jobs")
        )
        due_at = case(
            (
                memory_jobs.c.status == MemoryJobStatus.PENDING.value,
                memory_jobs.c.next_attempt_at,
            ),
            else_=memory_jobs.c.lease_expires_at,
        )
        return (
            select(
                memory_jobs.c.event_id,
                memory_jobs.c.boundary_message_id,
                memory_jobs.c.schema_version,
                memory_jobs.c.status,
                memory_jobs.c.attempt_count,
                conversation_messages.c.conversation_id,
                conversation_messages.c.turn_id,
                conversations.c.user_id,
                conversations.c.session_id,
            )
            .select_from(
                memory_jobs.join(
                    conversation_messages,
                    memory_jobs.c.boundary_message_id == conversation_messages.c.message_id,
                ).join(
                    conversations,
                    conversation_messages.c.conversation_id == conversations.c.conversation_id,
                )
            )
            .where(
                memory_jobs.c.attempt_count < max_attempts,
                or_(
                    (
                        (memory_jobs.c.status == MemoryJobStatus.PENDING.value)
                        & (memory_jobs.c.next_attempt_at <= func.now())
                    ),
                    (
                        (memory_jobs.c.status == MemoryJobStatus.PROCESSING.value)
                        & (memory_jobs.c.lease_expires_at <= func.now())
                    ),
                ),
            )
            .order_by(due_at.asc(), memory_jobs.c.created_at.asc(), memory_jobs.c.event_id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True, of=memory_jobs)
            .add_cte(dead_exhausted)
        )

    @staticmethod
    async def _lease_candidate(
        connection: AsyncConnection,
        row: Mapping[str, Any],
        lease_owner: UUID,
        lease_seconds: float,
    ) -> MemoryJob:
        try:
            prior_status = MemoryJobStatus(row["status"])
            attempt_count = row["attempt_count"] + 1
            lease_token = uuid4()
            updated = (
                (
                    await connection.execute(
                        update(memory_jobs)
                        .where(
                            memory_jobs.c.event_id == row["event_id"],
                            memory_jobs.c.status == prior_status.value,
                        )
                        .values(
                            status=MemoryJobStatus.PROCESSING.value,
                            attempt_count=attempt_count,
                            lease_owner=lease_owner,
                            lease_token=lease_token,
                            lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                            updated_at=func.now(),
                        )
                        .returning(
                            memory_jobs.c.attempt_count,
                            memory_jobs.c.lease_token,
                            memory_jobs.c.lease_expires_at,
                        )
                    )
                )
                .mappings()
                .one()
            )
            reference = CompletedTurnReference(
                user_id=row["user_id"],
                session_id=row["session_id"],
                conversation_id=row["conversation_id"],
                turn_id=row["turn_id"],
                boundary_message_id=row["boundary_message_id"],
            )
            return MemoryJob(
                event_id=row["event_id"],
                reference=reference,
                attempt_count=updated["attempt_count"],
                lease_token=updated["lease_token"],
                lease_expires_at=updated["lease_expires_at"],
                reclaimed=prior_status is MemoryJobStatus.PROCESSING,
                schema_version=row["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MemoryJobQueueProtocolError from error

    async def _leased_transition(
        self,
        event_id: UUID,
        lease_token: UUID,
        values: dict[str, object],
    ) -> None:
        statement = (
            update(memory_jobs)
            .where(
                memory_jobs.c.event_id == event_id,
                memory_jobs.c.status == MemoryJobStatus.PROCESSING.value,
                memory_jobs.c.lease_token == lease_token,
                memory_jobs.c.lease_expires_at > func.now(),
            )
            .values(**values)
        )
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(statement)
                if result.rowcount != 1:
                    raise MemoryJobLeaseLostError
        except MemoryJobLeaseLostError:
            raise
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    @staticmethod
    def _to_dead_job(row: Mapping[str, Any]) -> DeadMemoryJob:
        return DeadMemoryJob(
            event_id=row["event_id"],
            attempt_count=row["attempt_count"],
            requeue_count=row["requeue_count"],
            last_error_class=row["last_error_class"],
            created_at=row["created_at"],
            dead_at=row["dead_at"],
        )

    @staticmethod
    def _raise_mapped(error: BaseException) -> NoReturn:
        sqlstate = _find_sqlstate(error)
        if sqlstate in _CONFIGURATION_SQLSTATES:
            raise MemoryJobQueueConfigurationError from error
        if _has_connection_failure(error):
            raise MemoryJobQueueConnectionError from error
        if isinstance(error, DBAPIError) and (
            error.connection_invalidated or _is_connection(sqlstate)
        ):
            raise MemoryJobQueueConnectionError from error
        raise MemoryJobQueueOperationError from error


def _require_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{field_name} must be a UUID")


def _require_positive_integer(
    value: object, field_name: str, *, maximum: int | None = None
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} exceeds the supported maximum")


def _require_nonnegative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")


def _require_positive_finite_number(value: object, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be finite and positive")


def _require_aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_error_class(value: object) -> None:
    if not isinstance(value, str) or _ERROR_CLASS_PATTERN.fullmatch(value) is None:
        raise ValueError("error_class must be a bounded ASCII class name")


def _find_sqlstate(error: BaseException) -> str | None:
    for current in _error_chain(error):
        value = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if isinstance(value, str):
            return value
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
