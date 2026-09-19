"""Static schema contract for fenced chat request reservations."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.infrastructure.postgres.schema import (
    CHAT_REQUEST_SCHEMA_REVISIONS,
    EXPECTED_SCHEMA_REVISION,
    chat_requests,
)


def test_chat_request_schema_is_available_only_at_current_revision() -> None:
    assert CHAT_REQUEST_SCHEMA_REVISIONS == {EXPECTED_SCHEMA_REVISION}
    assert list(chat_requests.c.keys()) == [
        "request_id",
        "conversation_id",
        "client_message_id",
        "content_hash",
        "turn_id",
        "status",
        "attempt_count",
        "lease_token",
        "lease_expires_at",
        "created_at",
        "updated_at",
        "completed_at",
    ]


def test_chat_request_schema_has_fencing_and_idempotency_constraints() -> None:
    checks = {
        constraint.name
        for constraint in chat_requests.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_chat_requests_attempt_count",
        "ck_chat_requests_content_hash",
        "ck_chat_requests_lease_expiry",
        "ck_chat_requests_lifecycle",
        "ck_chat_requests_status",
        "ck_chat_requests_turn_id_nonempty",
    }
    unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in chat_requests.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique == {
        "uq_chat_requests_conversation_client_message": (
            "conversation_id",
            "client_message_id",
        ),
        "uq_chat_requests_turn_id": ("turn_id",),
    }
    foreign_key = next(
        constraint
        for constraint in chat_requests.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert foreign_key.name == "fk_chat_requests_conversation"
    assert foreign_key.ondelete == "CASCADE"

    indexes = {index.name: index for index in chat_requests.indexes}
    assert set(indexes) == {
        "ix_chat_requests_processing_lease",
        "uq_chat_requests_conversation_processing",
    }
    assert indexes["uq_chat_requests_conversation_processing"].unique
    assert all(
        index.dialect_options["postgresql"]["where"] is not None for index in indexes.values()
    )
