"""SQLAlchemy Core schema for durable conversation messages."""

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import BYTEA, JSONB

PREVIOUS_SCHEMA_REVISION = "20260919_0007"
EXPECTED_SCHEMA_REVISION = "20260920_0008"
SUPPORTED_SCHEMA_REVISIONS = frozenset({PREVIOUS_SCHEMA_REVISION, EXPECTED_SCHEMA_REVISION})
TELEMETRY_CONTEXT_SCHEMA_REVISIONS = SUPPORTED_SCHEMA_REVISIONS
CONVERSATION_MANAGEMENT_SCHEMA_REVISIONS = SUPPORTED_SCHEMA_REVISIONS
CHAT_REQUEST_SCHEMA_REVISIONS = SUPPORTED_SCHEMA_REVISIONS
CONVERSATION_FEEDBACK_SCHEMA_REVISIONS = frozenset({EXPECTED_SCHEMA_REVISION})

metadata = MetaData()

auth_users = Table(
    "auth_users",
    metadata,
    Column("user_id", Uuid(as_uuid=True), primary_key=True),
    Column("username", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("failed_login_count", SmallInteger, nullable=False, server_default=text("0")),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "password_changed_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "username = lower(username) AND username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'",
        name="ck_auth_users_username_normalized",
    ),
    CheckConstraint("length(password_hash) > 0", name="ck_auth_users_password_hash_nonempty"),
    CheckConstraint("failed_login_count >= 0", name="ck_auth_users_failed_login_count"),
    CheckConstraint(
        "locked_until IS NULL OR locked_until >= created_at",
        name="ck_auth_users_locked_until",
    ),
    UniqueConstraint("username", name="uq_auth_users_username"),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("session_id", Uuid(as_uuid=True), primary_key=True),
    Column(
        "user_id",
        Uuid(as_uuid=True),
        ForeignKey("auth_users.user_id", name="fk_auth_sessions_user", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("token_hash", BYTEA, nullable=False),
    Column("csrf_token_hash", BYTEA, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("idle_expires_at", DateTime(timezone=True), nullable=False),
    Column("absolute_expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("octet_length(token_hash) = 32", name="ck_auth_sessions_token_hash"),
    CheckConstraint("octet_length(csrf_token_hash) = 32", name="ck_auth_sessions_csrf_hash"),
    CheckConstraint("last_seen_at >= created_at", name="ck_auth_sessions_last_seen"),
    CheckConstraint(
        "idle_expires_at > created_at AND idle_expires_at <= absolute_expires_at",
        name="ck_auth_sessions_expiry",
    ),
    CheckConstraint(
        "revoked_at IS NULL OR revoked_at >= created_at",
        name="ck_auth_sessions_revoked_at",
    ),
    UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
)

Index(
    "ix_auth_sessions_user_active",
    auth_sessions.c.user_id,
    auth_sessions.c.absolute_expires_at,
    postgresql_where=auth_sessions.c.revoked_at.is_(None),
)

Index(
    "ix_auth_sessions_expired_cleanup",
    auth_sessions.c.absolute_expires_at,
    auth_sessions.c.session_id,
)

conversations = Table(
    "conversations",
    metadata,
    Column("conversation_id", Uuid(as_uuid=True), primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("title", Text, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("next_turn_sequence", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_message_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("next_turn_sequence >= 1", name="ck_conversations_next_turn_sequence"),
    CheckConstraint(
        "title IS NULL OR (title = btrim(title) AND length(title) BETWEEN 1 AND 200)",
        name="ck_conversations_title",
    ),
    CheckConstraint(
        "status IN ('active', 'deletion_pending')",
        name="ck_conversations_status",
    ),
    UniqueConstraint("user_id", "session_id", name="uq_conversations_user_session"),
)

Index(
    "ix_conversations_user_activity",
    conversations.c.user_id,
    func.coalesce(conversations.c.last_message_at, conversations.c.created_at).desc(),
    conversations.c.conversation_id.desc(),
)

chat_requests = Table(
    "chat_requests",
    metadata,
    Column("request_id", Uuid(as_uuid=True), primary_key=True),
    Column(
        "conversation_id",
        Uuid(as_uuid=True),
        ForeignKey(
            "conversations.conversation_id",
            name="fk_chat_requests_conversation",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("client_message_id", Uuid(as_uuid=True), nullable=False),
    Column("content_hash", BYTEA, nullable=False),
    Column("turn_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempt_count", SmallInteger, nullable=False, server_default=text("1")),
    Column("lease_token", Uuid(as_uuid=True), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("octet_length(content_hash) = 32", name="ck_chat_requests_content_hash"),
    CheckConstraint("length(turn_id) > 0", name="ck_chat_requests_turn_id_nonempty"),
    CheckConstraint("attempt_count >= 1", name="ck_chat_requests_attempt_count"),
    CheckConstraint(
        "status IN ('processing', 'completed', 'failed', 'cancelled')",
        name="ck_chat_requests_status",
    ),
    CheckConstraint(
        "(status = 'processing' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
        "AND completed_at IS NULL) OR "
        "(status IN ('failed', 'cancelled') AND lease_token IS NULL "
        "AND lease_expires_at IS NULL AND completed_at IS NULL) OR "
        "(status = 'completed' AND lease_token IS NULL AND lease_expires_at IS NULL "
        "AND completed_at IS NOT NULL)",
        name="ck_chat_requests_lifecycle",
    ),
    CheckConstraint(
        "lease_expires_at IS NULL OR lease_expires_at > updated_at",
        name="ck_chat_requests_lease_expiry",
    ),
    UniqueConstraint(
        "conversation_id",
        "client_message_id",
        name="uq_chat_requests_conversation_client_message",
    ),
    UniqueConstraint("turn_id", name="uq_chat_requests_turn_id"),
)

Index(
    "uq_chat_requests_conversation_processing",
    chat_requests.c.conversation_id,
    unique=True,
    postgresql_where=chat_requests.c.status == "processing",
)
Index(
    "ix_chat_requests_processing_lease",
    chat_requests.c.lease_expires_at,
    chat_requests.c.request_id,
    postgresql_where=chat_requests.c.status == "processing",
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

conversation_feedback = Table(
    "conversation_feedback",
    metadata,
    Column("feedback_id", Uuid(as_uuid=True), primary_key=True),
    Column(
        "conversation_id",
        Uuid(as_uuid=True),
        ForeignKey(
            "conversations.conversation_id",
            name="fk_conversation_feedback_conversation",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("turn_id", Text, nullable=False),
    Column("rating", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("length(turn_id) > 0", name="ck_conversation_feedback_turn_nonempty"),
    CheckConstraint("rating IN ('up', 'down')", name="ck_conversation_feedback_rating"),
    UniqueConstraint(
        "conversation_id",
        "turn_id",
        name="uq_conversation_feedback_turn",
    ),
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
    Column("telemetry_context", JSONB, nullable=True),
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
