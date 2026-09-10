"""Real PostgreSQL acceptance for the bounded memory-job operator CLI."""

import json
import os
from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory_job import MemoryJobStatus
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import conversations, memory_jobs
from worker.job_admin import AdminExitCode, parse_args, run
from worker.settings import MemoryJobAdminSettings

pytestmark = pytest.mark.postgres_integration


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


@pytest.fixture(scope="module")
def migrated_database() -> str:
    database_url = _test_url()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    return database_url


async def test_operator_cli_stats_dead_listing_and_explicit_requeue(
    migrated_database: str,
) -> None:
    user_id = f"t4-16-user-{uuid4()}"
    session_id = f"t4-16-session-{uuid4()}"
    turn_id = f"t4-16-turn-{uuid4()}"
    question = "private synthetic question"
    answer = "private synthetic answer"
    timestamp = datetime.now(UTC)
    engine = create_async_engine(migrated_database, pool_pre_ping=True)
    try:
        stored = await PostgresConversationStoreAdapter(engine).append_turn(
            user_id,
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.USER,
                question,
                timestamp,
            ),
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.ASSISTANT,
                answer,
                timestamp + timedelta(milliseconds=1),
            ),
            schedule_memory=True,
        )
        assert stored.memory_job_event_id is not None
        queue = PostgresMemoryJobQueueAdapter(engine)
        claimed = (
            await queue.claim_due(
                lease_owner=uuid4(),
                limit=1,
                lease_seconds=120,
                max_attempts=5,
            )
        )[0]
        assert claimed.event_id == stored.memory_job_event_id
        await queue.dead_letter(
            claimed.event_id,
            claimed.lease_token,
            error_class="LongTermMemoryTimeoutError",
        )

        settings = MemoryJobAdminSettings(
            _env_file=None,
            database_url=migrated_database,
            memory_job_db_timeout_seconds=5,
        )
        stats_output = StringIO()
        assert (
            await run(parse_args(["stats"]), settings=settings, output=stats_output)
            is AdminExitCode.SUCCESS
        )
        assert json.loads(stats_output.getvalue())["dead"] >= 1

        dead_output = StringIO()
        assert (
            await run(
                parse_args(["list-dead", "--limit", "1000"]),
                settings=settings,
                output=dead_output,
            )
            is AdminExitCode.SUCCESS
        )
        dead_payload = json.loads(dead_output.getvalue())
        matching = [
            item
            for item in dead_payload["items"]
            if item["event_id"] == str(stored.memory_job_event_id)
        ]
        assert len(matching) == 1
        assert matching[0]["attempt_count"] == 1
        assert matching[0]["requeue_count"] == 0
        assert matching[0]["last_error_class"] == "LongTermMemoryTimeoutError"
        rendered = dead_output.getvalue()
        for forbidden in (user_id, session_id, turn_id, question, answer, "boundary_message_id"):
            assert forbidden not in rendered

        requeue_output = StringIO()
        assert (
            await run(
                parse_args(["requeue", "--event-id", str(stored.memory_job_event_id)]),
                settings=settings,
                output=requeue_output,
            )
            is AdminExitCode.SUCCESS
        )
        assert json.loads(requeue_output.getvalue())["requeued"] is True

        second_output = StringIO()
        assert (
            await run(
                parse_args(["requeue", "--event-id", str(stored.memory_job_event_id)]),
                settings=settings,
                output=second_output,
            )
            is AdminExitCode.NOT_REQUEUED
        )
        assert json.loads(second_output.getvalue())["requeued"] is False

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        memory_jobs.c.status,
                        memory_jobs.c.attempt_count,
                        memory_jobs.c.requeue_count,
                        memory_jobs.c.last_error_class,
                        memory_jobs.c.dead_at,
                    ).where(memory_jobs.c.event_id == stored.memory_job_event_id)
                )
            ).one()
        assert row.status == MemoryJobStatus.PENDING.value
        assert row.attempt_count == 0
        assert row.requeue_count == 1
        assert row.last_error_class is None
        assert row.dead_at is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
            )
        await engine.dispose()
