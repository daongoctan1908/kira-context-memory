"""Static contract tests for managed conversations."""

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.infrastructure.postgres.schema import (
    CONVERSATION_MANAGEMENT_SCHEMA_REVISIONS,
    EXPECTED_SCHEMA_REVISION,
    PREVIOUS_SCHEMA_REVISION,
    conversations,
)


def test_conversation_management_schema_is_current_only() -> None:
    assert CONVERSATION_MANAGEMENT_SCHEMA_REVISIONS == {
        PREVIOUS_SCHEMA_REVISION,
        EXPECTED_SCHEMA_REVISION,
    }
    assert list(conversations.c.keys()) == [
        "conversation_id",
        "user_id",
        "session_id",
        "title",
        "status",
        "next_turn_sequence",
        "created_at",
        "updated_at",
        "last_message_at",
    ]


def test_conversations_have_lifecycle_constraints_and_activity_index() -> None:
    checks = {
        constraint.name
        for constraint in conversations.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_conversations_next_turn_sequence",
        "ck_conversations_status",
        "ck_conversations_title",
    }
    unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in conversations.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique == {"uq_conversations_user_session": ("user_id", "session_id")}
    assert {index.name for index in conversations.indexes} == {"ix_conversations_user_activity"}
