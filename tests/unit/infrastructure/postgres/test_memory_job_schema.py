"""Static contract tests for the PostgreSQL memory-job schema."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.infrastructure.postgres.schema import EXPECTED_SCHEMA_REVISION, memory_jobs


def test_memory_job_schema_is_reference_only_and_versioned() -> None:
    assert EXPECTED_SCHEMA_REVISION == "20260908_0003"
    assert list(memory_jobs.c.keys()) == [
        "event_id",
        "boundary_message_id",
        "schema_version",
        "status",
        "attempt_count",
        "requeue_count",
        "next_attempt_at",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "last_error_class",
        "lifecycle_event_count",
        "created_at",
        "updated_at",
        "completed_at",
        "dead_at",
    ]
    assert memory_jobs.c.schema_version.server_default is not None
    assert memory_jobs.c.status.server_default is not None
    assert memory_jobs.c.attempt_count.server_default is not None
    assert memory_jobs.c.requeue_count.server_default is not None
    assert memory_jobs.c.next_attempt_at.server_default is not None
    assert not {
        "content",
        "prompt",
        "provider_response",
        "memory",
        "user_id",
        "session_id",
    }.intersection(memory_jobs.c.keys())


def test_memory_job_schema_has_boundary_and_lifecycle_constraints() -> None:
    check_names = {
        constraint.name
        for constraint in memory_jobs.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert check_names == {
        "ck_memory_jobs_attempt_state",
        "ck_memory_jobs_counts",
        "ck_memory_jobs_error_class",
        "ck_memory_jobs_lease_state",
        "ck_memory_jobs_schema_version",
        "ck_memory_jobs_status",
        "ck_memory_jobs_terminal_state",
        "ck_memory_jobs_terminal_timestamps",
    }

    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in memory_jobs.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints["uq_memory_jobs_boundary_message"] == ("boundary_message_id",)

    foreign_keys = [
        constraint
        for constraint in memory_jobs.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    assert len(foreign_keys) == 1
    assert foreign_keys[0].name == "fk_memory_jobs_boundary_message"
    assert foreign_keys[0].ondelete == "CASCADE"
    assert tuple(element.target_fullname for element in foreign_keys[0].elements) == (
        "conversation_messages.message_id",
    )


def test_memory_job_schema_has_partial_operational_indexes() -> None:
    indexes = {index.name: index for index in memory_jobs.indexes}
    assert set(indexes) == {
        "ix_memory_jobs_completed_cleanup",
        "ix_memory_jobs_dead_cleanup",
        "ix_memory_jobs_pending_due",
        "ix_memory_jobs_processing_lease",
    }
    assert tuple(column.name for column in indexes["ix_memory_jobs_pending_due"].columns) == (
        "next_attempt_at",
        "created_at",
        "event_id",
    )
    assert tuple(column.name for column in indexes["ix_memory_jobs_processing_lease"].columns) == (
        "lease_expires_at",
        "event_id",
    )
    for index in indexes.values():
        assert index.dialect_options["postgresql"]["where"] is not None
