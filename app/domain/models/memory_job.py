"""Framework-free models for the PostgreSQL asynchronous memory-job queue."""

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.models.conversation import CompletedTurnReference

MEMORY_JOB_SCHEMA_VERSION = 1


class MemoryJobStatus(StrEnum):
    """Persisted lifecycle states for one durable memory job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class MemoryJob:
    """One claimed job tied to an exact completed conversation boundary."""

    event_id: UUID
    reference: CompletedTurnReference = field(repr=False)
    attempt_count: int
    lease_token: UUID
    lease_expires_at: datetime
    reclaimed: bool = False
    schema_version: int = MEMORY_JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_uuid(self.event_id, "event_id")
        if not isinstance(self.reference, CompletedTurnReference):
            raise ValueError("reference must be a completed turn reference")
        _require_positive_integer(self.attempt_count, "attempt_count")
        _require_uuid(self.lease_token, "lease_token")
        _require_aware_datetime(self.lease_expires_at, "lease_expires_at")
        if not isinstance(self.reclaimed, bool):
            raise ValueError("reclaimed must be a boolean")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != MEMORY_JOB_SCHEMA_VERSION
        ):
            raise ValueError("unsupported memory job schema version")


@dataclass(frozen=True, slots=True)
class MemoryJobStats:
    """Low-cardinality queue state suitable for metrics and admin output."""

    pending: int
    processing: int
    completed: int
    dead: int
    oldest_pending_age_seconds: float | None = None

    def __post_init__(self) -> None:
        for field_name in ("pending", "processing", "completed", "dead"):
            _require_nonnegative_integer(getattr(self, field_name), field_name)
        age = self.oldest_pending_age_seconds
        if age is not None and (
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not math.isfinite(age)
            or age < 0
        ):
            raise ValueError("oldest_pending_age_seconds must be finite and nonnegative")


@dataclass(frozen=True, slots=True)
class DeadMemoryJob:
    """Sanitized dead-job projection; it deliberately contains no conversation data."""

    event_id: UUID
    attempt_count: int
    requeue_count: int
    last_error_class: str
    created_at: datetime
    dead_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.event_id, "event_id")
        _require_positive_integer(self.attempt_count, "attempt_count")
        _require_nonnegative_integer(self.requeue_count, "requeue_count")
        if (
            not isinstance(self.last_error_class, str)
            or not self.last_error_class.isidentifier()
            or len(self.last_error_class) > 128
        ):
            raise ValueError("last_error_class must be a bounded class name")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.dead_at, "dead_at")
        if self.dead_at < self.created_at:
            raise ValueError("dead_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class MemoryJobPurgeResult:
    """Counts removed by one bounded terminal-job cleanup operation."""

    completed: int = 0
    dead: int = 0

    def __post_init__(self) -> None:
        _require_nonnegative_integer(self.completed, "completed")
        _require_nonnegative_integer(self.dead, "dead")


def _require_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{field_name} must be a UUID")


def _require_positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_nonnegative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")


def _require_aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
