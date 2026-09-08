"""SQLAlchemy Core schema for durable conversation messages."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)

EXPECTED_SCHEMA_REVISION = "20260908_0003"

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

memory_jobs = Table(
    "memory_jobs",
    metadata,
    Column("event_id", Uuid(as_uuid=True), primary_key=True),
    Column(
        "boundary_message_id",
        BigInteger,
        ForeignKey(
            "conversation_messages.message_id",
            name="fk_memory_jobs_boundary_message",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("schema_version", SmallInteger, nullable=False, server_default=text("1")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("attempt_count", SmallInteger, nullable=False, server_default=text("0")),
    Column("requeue_count", SmallInteger, nullable=False, server_default=text("0")),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_owner", Uuid(as_uuid=True), nullable=True),
    Column("lease_token", Uuid(as_uuid=True), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error_class", Text, nullable=True),
    Column("lifecycle_event_count", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("dead_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("schema_version = 1", name="ck_memory_jobs_schema_version"),
    CheckConstraint(
        "attempt_count >= 0 AND requeue_count >= 0 "
        "AND (lifecycle_event_count IS NULL OR lifecycle_event_count >= 0)",
        name="ck_memory_jobs_counts",
    ),
    CheckConstraint(
        "status = 'pending' OR attempt_count >= 1",
        name="ck_memory_jobs_attempt_state",
    ),
    CheckConstraint(
        "last_error_class IS NULL OR last_error_class ~ '^[A-Za-z_][A-Za-z0-9_]{0,127}$'",
        name="ck_memory_jobs_error_class",
    ),
    CheckConstraint(
        "status IN ('pending', 'processing', 'completed', 'dead')",
        name="ck_memory_jobs_status",
    ),
    CheckConstraint(
        "(status = 'processing' AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (status <> 'processing' AND lease_owner IS NULL "
        "AND lease_token IS NULL AND lease_expires_at IS NULL)",
        name="ck_memory_jobs_lease_state",
    ),
    CheckConstraint(
        "(status = 'pending' AND completed_at IS NULL AND dead_at IS NULL "
        "AND lifecycle_event_count IS NULL) "
        "OR (status = 'processing' AND completed_at IS NULL AND dead_at IS NULL "
        "AND lifecycle_event_count IS NULL) "
        "OR (status = 'completed' AND completed_at IS NOT NULL AND dead_at IS NULL "
        "AND lifecycle_event_count IS NOT NULL) "
        "OR (status = 'dead' AND completed_at IS NULL AND dead_at IS NOT NULL "
        "AND lifecycle_event_count IS NULL AND last_error_class IS NOT NULL)",
        name="ck_memory_jobs_terminal_state",
    ),
    CheckConstraint(
        "(completed_at IS NULL OR completed_at >= created_at) "
        "AND (dead_at IS NULL OR dead_at >= created_at)",
        name="ck_memory_jobs_terminal_timestamps",
    ),
    UniqueConstraint("boundary_message_id", name="uq_memory_jobs_boundary_message"),
)

Index(
    "ix_memory_jobs_pending_due",
    memory_jobs.c.next_attempt_at,
    memory_jobs.c.created_at,
    memory_jobs.c.event_id,
    postgresql_where=memory_jobs.c.status == "pending",
)
Index(
    "ix_memory_jobs_processing_lease",
    memory_jobs.c.lease_expires_at,
    memory_jobs.c.event_id,
    postgresql_where=memory_jobs.c.status == "processing",
)
Index(
    "ix_memory_jobs_completed_cleanup",
    memory_jobs.c.completed_at,
    memory_jobs.c.event_id,
    postgresql_where=memory_jobs.c.status == "completed",
)
Index(
    "ix_memory_jobs_dead_cleanup",
    memory_jobs.c.dead_at,
    memory_jobs.c.event_id,
    postgresql_where=memory_jobs.c.status == "dead",
)
