"""SQLAlchemy Core schema for durable conversation messages."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)

EXPECTED_SCHEMA_REVISION = "20260906_0002"

metadata = MetaData()

conversations = Table(
    "conversations",
    metadata,
    Column("conversation_id", Uuid(as_uuid=True), primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("next_turn_sequence", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("next_turn_sequence >= 1", name="ck_conversations_next_turn_sequence"),
    UniqueConstraint("user_id", "session_id", name="uq_conversations_user_session"),
)

conversation_messages = Table(
    "conversation_messages",
    metadata,
    Column("message_id", BigInteger, Identity(), primary_key=True),
    Column(
        "conversation_id",
        Uuid(as_uuid=True),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("turn_id", Text, nullable=False),
    Column("turn_sequence", BigInteger, nullable=False),
    Column("message_index", SmallInteger, nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("message_timestamp", DateTime(timezone=True), nullable=False),
    Column("schema_version", SmallInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("turn_sequence >= 1", name="ck_messages_turn_sequence"),
    CheckConstraint("message_index IN (0, 1)", name="ck_messages_message_index"),
    CheckConstraint(
        "(message_index = 0 AND role = 'user') OR (message_index = 1 AND role = 'assistant')",
        name="ck_messages_role_position",
    ),
    CheckConstraint("length(content) > 0", name="ck_messages_content_nonempty"),
    UniqueConstraint("turn_id", "message_index", name="uq_messages_turn_position"),
    UniqueConstraint(
        "conversation_id",
        "turn_sequence",
        "message_index",
        name="uq_messages_conversation_sequence_position",
    ),
)

Index(
    "ix_messages_conversation_recent",
    conversation_messages.c.conversation_id,
    conversation_messages.c.turn_sequence.desc(),
    conversation_messages.c.message_index.desc(),
)
