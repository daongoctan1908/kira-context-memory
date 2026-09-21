"""Static contract tests for persisted assistant feedback."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.infrastructure.postgres.schema import (
    CONVERSATION_FEEDBACK_SCHEMA_REVISIONS,
    EXPECTED_SCHEMA_REVISION,
    conversation_feedback,
)


def test_feedback_is_available_at_the_new_revision() -> None:
    assert CONVERSATION_FEEDBACK_SCHEMA_REVISIONS == {EXPECTED_SCHEMA_REVISION}
    assert list(conversation_feedback.c.keys()) == [
        "feedback_id",
        "conversation_id",
        "turn_id",
        "rating",
        "created_at",
        "updated_at",
    ]


def test_feedback_has_rating_ownership_and_one_verdict_per_turn() -> None:
    checks = {
        constraint.name
        for constraint in conversation_feedback.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_conversation_feedback_rating",
        "ck_conversation_feedback_turn_nonempty",
    }
    unique = next(
        constraint
        for constraint in conversation_feedback.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    assert unique.name == "uq_conversation_feedback_turn"
    assert tuple(column.name for column in unique.columns) == ("conversation_id", "turn_id")
    foreign_key = next(
        constraint
        for constraint in conversation_feedback.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert foreign_key.name == "fk_conversation_feedback_conversation"
    assert foreign_key.ondelete == "CASCADE"
