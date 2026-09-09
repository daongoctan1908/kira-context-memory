import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.errors.conversation import (
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.errors.kira import KiraTimeoutError
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.postgres import conversation_store as conversation_store_module
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    conversation_messages,
    conversations,
    memory_jobs,
)
from app.presentation.api.main import create_app
from tests.integration.test_gateway_api import FakeKiraClient, kira_event, make_settings
from tests.support.context_fakes import FakeRewriter
from tests.support.mock_kira_server import app as mock_kira_app
from tests.support.mock_kira_server import query_hashes
from tests.support.mock_vllm_server import app as mock_vllm_app
from tests.support.week2_cases import CASES

pytestmark = pytest.mark.postgres_integration
USER_ID = "test-user"


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


@pytest.fixture
async def engine(migrated_database: str):
    value = create_async_engine(migrated_database, pool_pre_ping=True)
    try:
        yield value
    finally:
        await value.dispose()


@pytest.fixture
async def session_id(engine: AsyncEngine):
    value = f"pg-test-{uuid4()}"
    try:
        yield value
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(conversations.c.session_id == value)
            )


def _message(
    session_id: str,
    turn_id: str,
    role: ConversationRole,
    content: str,
    offset: int,
) -> ConversationMessage:
    return ConversationMessage(
        session_id=session_id,
        turn_id=turn_id,
        role=role,
        content=content,
        timestamp=datetime(2026, 9, 3, 9, 0, tzinfo=UTC) + timedelta(seconds=offset),
    )


async def _append(
    adapter: PostgresConversationStoreAdapter,
    session_id: str,
    turn_number: int,
    *,
    turn_id: str | None = None,
) -> bool:
    resolved_turn_id = turn_id or f"{session_id}-turn-{turn_number}"
    result = await adapter.append_turn(
        USER_ID,
        _message(
            session_id,
            resolved_turn_id,
            ConversationRole.USER,
            f"Câu hỏi tiếng Việt {turn_number}",
            turn_number * 2,
        ),
        _message(
            session_id,
            resolved_turn_id,
            ConversationRole.ASSISTANT,
            f"Trả lời tiếng Việt {turn_number}",
            turn_number * 2 + 1,
        ),
    )
    return result.inserted


async def _session_persistence_counts(
    engine: AsyncEngine,
    session_id: str,
) -> tuple[int, int, int]:
    filters = (
        conversations.c.user_id == USER_ID,
        conversations.c.session_id == session_id,
    )
    async with engine.connect() as connection:
        conversation_count = await connection.scalar(
            select(func.count()).select_from(conversations).where(*filters)
        )
        message_count = await connection.scalar(
            select(func.count())
            .select_from(conversation_messages.join(conversations))
            .where(*filters)
        )
        job_count = await connection.scalar(
            select(func.count())
            .select_from(
                memory_jobs.join(
                    conversation_messages,
                    memory_jobs.c.boundary_message_id == conversation_messages.c.message_id,
                ).join(conversations)
            )
            .where(*filters)
        )
    return (
        int(conversation_count or 0),
        int(message_count or 0),
        int(job_count or 0),
    )


async def test_migration_revision_and_recent_index_exist(engine: AsyncEngine) -> None:
    adapter = PostgresConversationStoreAdapter(engine)

    await adapter.validate_schema()
    async with engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        index_definition = await connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'ix_messages_conversation_recent'"
            )
        )

    assert revision == EXPECTED_SCHEMA_REVISION
    assert index_definition is not None
    assert "conversation_id" in index_definition
    assert "turn_sequence DESC" in index_definition
    assert "message_index DESC" in index_definition


async def test_full_history_is_retained_while_recent_read_is_bounded(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)

    for turn_number in range(1, 7):
        assert await _append(adapter, session_id, turn_number) is True

    recent = await adapter.read_recent(USER_ID, session_id, 10)
    async with engine.connect() as connection:
        persisted_count = await connection.scalar(
            select(text("count(*)"))
            .select_from(conversation_messages.join(conversations))
            .where(conversations.c.session_id == session_id)
        )
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        query_plan = (
            (
                await connection.execute(
                    text(
                        "EXPLAIN SELECT m.* FROM conversation_messages AS m "
                        "JOIN conversations AS c ON c.conversation_id = m.conversation_id "
                        "WHERE c.session_id = :session_id "
                        "ORDER BY m.turn_sequence DESC, m.message_index DESC LIMIT 10"
                    ),
                    {"session_id": session_id},
                )
            )
            .scalars()
            .all()
        )

    assert persisted_count == 12
    assert len(recent) == 10
    assert recent[0].content == "Câu hỏi tiếng Việt 2"
    assert recent[-1].content == "Trả lời tiếng Việt 6"
    assert any("ix_messages_conversation_recent" in line for line in query_plan)
    assert [item.role for item in recent] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ] * 5


async def test_duplicate_is_idempotent_and_collision_is_rejected(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    turn_id = f"global-{uuid4()}"

    assert await _append(adapter, session_id, 1, turn_id=turn_id) is True
    assert await _append(adapter, session_id, 1, turn_id=turn_id) is False

    with pytest.raises(ConversationStoreProtocolError):
        await adapter.append_turn(
            USER_ID,
            _message(session_id, turn_id, ConversationRole.USER, "different", 2),
            _message(session_id, turn_id, ConversationRole.ASSISTANT, "answer", 3),
        )

    assert len(await adapter.read_recent(USER_ID, session_id, 10)) == 2


async def test_completed_turn_and_memory_job_are_atomic_and_idempotent(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    turn_id = f"scheduled-{uuid4()}"
    user = _message(session_id, turn_id, ConversationRole.USER, "durable preference", 1)
    assistant = _message(session_id, turn_id, ConversationRole.ASSISTANT, "confirmed", 2)

    inserted = await adapter.append_turn(
        USER_ID,
        user,
        assistant,
        schedule_memory=True,
    )
    duplicate = await adapter.append_turn(
        USER_ID,
        user,
        assistant,
        schedule_memory=True,
    )

    async with engine.connect() as connection:
        job_rows = (
            (
                await connection.execute(
                    select(memory_jobs).where(
                        memory_jobs.c.boundary_message_id == inserted.reference.boundary_message_id
                    )
                )
            )
            .mappings()
            .all()
        )
        message_count = await connection.scalar(
            select(func.count())
            .select_from(conversation_messages)
            .where(conversation_messages.c.turn_id == turn_id)
        )

    assert inserted.inserted is True
    assert duplicate.inserted is False
    assert inserted.memory_job_event_id is not None
    assert duplicate.memory_job_event_id == inserted.memory_job_event_id
    assert message_count == 2
    assert len(job_rows) == 1
    assert job_rows[0]["event_id"] == inserted.memory_job_event_id
    assert job_rows[0]["status"] == "pending"
    assert job_rows[0]["attempt_count"] == 0


async def test_completed_turn_does_not_schedule_when_formation_is_disabled(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    turn_id = f"not-scheduled-{uuid4()}"
    result = await adapter.append_turn(
        USER_ID,
        _message(session_id, turn_id, ConversationRole.USER, "question", 1),
        _message(session_id, turn_id, ConversationRole.ASSISTANT, "answer", 2),
    )

    async with engine.connect() as connection:
        event_id = await connection.scalar(
            select(memory_jobs.c.event_id).where(
                memory_jobs.c.boundary_message_id == result.reference.boundary_message_id
            )
        )

    assert result.memory_job_event_id is None
    assert event_id is None


async def test_duplicate_old_turn_is_not_backfilled_after_formation_is_enabled(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    turn_id = f"not-backfilled-{uuid4()}"
    user = _message(session_id, turn_id, ConversationRole.USER, "question", 1)
    assistant = _message(session_id, turn_id, ConversationRole.ASSISTANT, "answer", 2)
    original = await adapter.append_turn(USER_ID, user, assistant)

    duplicate = await adapter.append_turn(
        USER_ID,
        user,
        assistant,
        schedule_memory=True,
    )

    async with engine.connect() as connection:
        event_id = await connection.scalar(
            select(memory_jobs.c.event_id).where(
                memory_jobs.c.boundary_message_id == original.reference.boundary_message_id
            )
        )
    assert original.inserted is True
    assert original.memory_job_event_id is None
    assert duplicate.inserted is False
    assert duplicate.reference == original.reference
    assert duplicate.memory_job_event_id is None
    assert event_id is None


async def test_memory_job_insert_failure_rolls_back_new_conversation_turn(
    engine: AsyncEngine,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    seed_turn_id = f"seed-{uuid4()}"
    seed = await adapter.append_turn(
        USER_ID,
        _message(session_id, seed_turn_id, ConversationRole.USER, "seed", 1),
        _message(session_id, seed_turn_id, ConversationRole.ASSISTANT, "seed", 2),
        schedule_memory=True,
    )
    assert seed.memory_job_event_id is not None

    failing_session = f"job-rollback-{uuid4()}"
    failing_turn_id = f"job-rollback-{uuid4()}"
    monkeypatch.setattr(
        conversation_store_module,
        "uuid4",
        lambda: seed.memory_job_event_id,
    )

    with pytest.raises(ConversationStoreOperationError):
        await adapter.append_turn(
            USER_ID,
            _message(failing_session, failing_turn_id, ConversationRole.USER, "question", 3),
            _message(failing_session, failing_turn_id, ConversationRole.ASSISTANT, "answer", 4),
            schedule_memory=True,
        )

    async with engine.connect() as connection:
        conversation_count = await connection.scalar(
            select(func.count())
            .select_from(conversations)
            .where(conversations.c.session_id == failing_session)
        )
        message_count = await connection.scalar(
            select(func.count())
            .select_from(conversation_messages)
            .where(conversation_messages.c.turn_id == failing_turn_id)
        )

    assert conversation_count == 0
    assert message_count == 0


async def test_gateway_completed_stream_persists_and_schedules_atomically(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    settings = make_settings().model_copy(
        update={
            "kira_username": "local-smoke",
            "memory_formation_enabled": True,
        }
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_kira_app)) as kira_http:
        app = create_app(
            settings=settings,
            http_client=kira_http,
            postgres_engine=engine,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway.test",
            ) as client,
        ):
            response = await client.post(
                "/chat",
                json={"session_id": session_id, "message": "remember completed turn"},
            )

    async with engine.connect() as connection:
        scheduled = (
            await connection.execute(
                select(
                    memory_jobs.c.event_id,
                    memory_jobs.c.status,
                    conversation_messages.c.message_index,
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
                .where(conversations.c.session_id == session_id)
            )
        ).one()

    assert response.status_code == 200
    assert "gateway_error" not in response.text
    assert scheduled.status == "pending"
    assert scheduled.message_index == 1


async def test_gateway_formation_disabled_persists_turn_without_job(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    settings = make_settings().model_copy(
        update={
            "kira_username": "local-smoke",
            "memory_formation_enabled": False,
        }
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_kira_app)) as kira_http:
        app = create_app(
            settings=settings,
            http_client=kira_http,
            postgres_engine=engine,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway.test",
            ) as client,
        ):
            response = await client.post(
                "/chat",
                json={"session_id": session_id, "message": "completed without formation"},
            )
            metrics = await client.get("/metrics")

    assert response.status_code == 200
    assert "gateway_error" not in response.text
    assert await _session_persistence_counts(engine, session_id) == (1, 2, 0)
    assert 'kira_memory_job_schedule_total{outcome="disabled"} 1.0' in metrics.text


async def test_gateway_midstream_failure_persists_neither_turn_nor_job(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    kira_client = FakeKiraClient(
        events=[kira_event('{"text":"partial"}', "partial")],
        stream_error=KiraTimeoutError(stage="chat stream"),
    )
    app = create_app(
        settings=make_settings().model_copy(update={"memory_formation_enabled": True}),
        kira_client=kira_client,
        postgres_engine=engine,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client,
    ):
        response = await client.post(
            "/chat",
            json={"session_id": session_id, "message": "failing stream"},
        )
        metrics = await client.get("/metrics")

    assert response.status_code == 200
    assert "gateway_error" in response.text
    assert await _session_persistence_counts(engine, session_id) == (0, 0, 0)
    assert "kira_memory_job_schedule_total{" not in metrics.text


async def test_gateway_missing_identity_persists_neither_turn_nor_job(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    settings = make_settings().model_copy(
        update={
            "kira_username": "local-smoke",
            "dev_static_identity_enabled": False,
            "dev_static_user_id": None,
            "memory_formation_enabled": True,
        }
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_kira_app)) as kira_http:
        app = create_app(
            settings=settings,
            http_client=kira_http,
            postgres_engine=engine,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway.test",
            ) as client,
        ):
            response = await client.post(
                "/chat",
                json={"session_id": session_id, "message": "anonymous completion"},
            )
            metrics = await client.get("/metrics")

    assert response.status_code == 200
    assert "gateway_error" not in response.text
    assert await _session_persistence_counts(engine, session_id) == (0, 0, 0)
    assert "kira_memory_job_schedule_total{" not in metrics.text


async def test_gateway_job_insert_failure_rolls_back_without_mutating_sse(
    engine: AsyncEngine,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_session = f"{session_id}-seed"
    seed_turn_id = f"seed-{uuid4()}"
    seed = await PostgresConversationStoreAdapter(engine).append_turn(
        USER_ID,
        _message(seed_session, seed_turn_id, ConversationRole.USER, "seed", 1),
        _message(seed_session, seed_turn_id, ConversationRole.ASSISTANT, "seed", 2),
        schedule_memory=True,
    )
    assert seed.memory_job_event_id is not None
    monkeypatch.setattr(
        conversation_store_module,
        "uuid4",
        lambda: seed.memory_job_event_id,
    )
    app = create_app(
        settings=make_settings().model_copy(update={"memory_formation_enabled": True}),
        kira_client=FakeKiraClient(events=[kira_event('{"text":"answer"}', "answer")]),
        postgres_engine=engine,
        query_rewriter=FakeRewriter(),
    )
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway.test",
            ) as client,
        ):
            response = await client.post(
                "/chat",
                json={"session_id": session_id, "message": "must roll back"},
            )
            metrics = await client.get("/metrics")

        assert response.content == b'data: {"text":"answer"}\n\n'
        assert await _session_persistence_counts(engine, session_id) == (0, 0, 0)
        assert 'kira_conversation_write_total{outcome="error"} 1.0' in metrics.text
        assert 'kira_memory_job_schedule_total{outcome="error"} 1.0' in metrics.text
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == USER_ID,
                    conversations.c.session_id == seed_session,
                )
            )


async def test_concurrent_append_preserves_pair_order_and_session_isolation(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    other_session = f"pg-test-{uuid4()}"
    try:
        results = await asyncio.gather(
            *(_append(adapter, session_id, turn_number) for turn_number in range(1, 6)),
            _append(adapter, other_session, 1),
        )

        assert all(results)
        recent = await adapter.read_recent(USER_ID, session_id, 10)
        other_recent = await adapter.read_recent(USER_ID, other_session, 10)
        assert len(recent) == 10
        assert len(other_recent) == 2
        for index in range(0, len(recent), 2):
            assert recent[index].role is ConversationRole.USER
            assert recent[index + 1].role is ConversationRole.ASSISTANT
            assert recent[index].turn_id == recent[index + 1].turn_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(conversations.c.session_id == other_session)
            )


async def test_same_session_is_isolated_by_user_and_boundary_is_exact(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    other_user = f"user-{uuid4()}"
    first_turn = f"turn-{uuid4()}"
    first = await adapter.append_turn(
        USER_ID,
        _message(session_id, first_turn, ConversationRole.USER, "first question", 1),
        _message(session_id, first_turn, ConversationRole.ASSISTANT, "first answer", 2),
    )
    later_turn = f"turn-{uuid4()}"
    await adapter.append_turn(
        USER_ID,
        _message(session_id, later_turn, ConversationRole.USER, "later", 3),
        _message(session_id, later_turn, ConversationRole.ASSISTANT, "later", 4),
    )
    other_turn = f"turn-{uuid4()}"
    await adapter.append_turn(
        other_user,
        _message(session_id, other_turn, ConversationRole.USER, "private question", 5),
        _message(session_id, other_turn, ConversationRole.ASSISTANT, "private answer", 6),
    )

    exact = await adapter.read_through_boundary(
        USER_ID,
        first.reference.conversation_id,
        first.reference.boundary_message_id,
        10,
    )

    assert [message.content for message in exact] == ["first question", "first answer"]
    assert [
        message.content for message in await adapter.read_recent(other_user, session_id, 10)
    ] == [
        "private question",
        "private answer",
    ]
    with pytest.raises(ConversationStoreProtocolError):
        await adapter.read_through_boundary(
            other_user,
            first.reference.conversation_id,
            first.reference.boundary_message_id,
            10,
        )


async def test_malformed_persisted_schema_maps_to_protocol_error(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    conversation_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(conversations).values(
                conversation_id=conversation_id,
                user_id=USER_ID,
                session_id=session_id,
                next_turn_sequence=2,
            )
        )
        await connection.execute(
            insert(conversation_messages),
            [
                {
                    "conversation_id": conversation_id,
                    "turn_id": f"bad-{uuid4()}",
                    "turn_sequence": 1,
                    "message_index": index,
                    "role": role,
                    "content": "invalid schema payload",
                    "message_timestamp": datetime.now(UTC),
                    "schema_version": 99,
                }
                for index, role in enumerate(("user", "assistant"))
            ],
        )

    with pytest.raises(ConversationStoreProtocolError):
        await PostgresConversationStoreAdapter(engine).read_recent(USER_ID, session_id, 10)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_full_flow_with_real_postgres_and_mock_http_adapters(engine, session_id, case):
    """Fixture-driven plumbing gate, not an evaluation of real Qwen semantics."""
    from hashlib import sha256

    settings = make_settings().model_copy(
        update={
            "kira_username": "local-smoke",
            "vllm_model": "local-contract-stub",
        }
    )
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_kira_app)) as kira_http,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_vllm_app)) as vllm_http,
    ):
        # Rebuild the Gateway between requests: history must survive in PostgreSQL.
        for query in (case.previous, case.current):
            app = create_app(
                settings=settings,
                http_client=kira_http,
                rewriter_http_client=vllm_http,
                postgres_engine=engine,
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://gateway.test",
                ) as client,
            ):
                response = await client.post(
                    "/chat", json={"session_id": session_id, "message": query}
                )
                assert response.status_code == 200
                assert "gateway_error" not in response.text
                assert (await client.get("/ready")).status_code == 200
                metrics = (await client.get("/metrics")).text
        assert 'kira_context_rewrite_total{outcome="success"} 1.0' in metrics
        assert query_hashes[-1] == sha256(case.expected.encode()).hexdigest()
    recent = await PostgresConversationStoreAdapter(engine).read_recent(USER_ID, session_id, 10)
    assert [message.content for message in recent] == [
        case.previous,
        "Mock KiRa answer",
        case.current,
        "Mock KiRa answer",
    ]
    assert recent[0].turn_id == recent[1].turn_id != recent[2].turn_id == recent[3].turn_id


async def test_cancellation_rolls_back_inflight_turn_and_allows_next_write(engine, session_id):
    adapter = PostgresConversationStoreAdapter(engine)
    locked = asyncio.Event()
    original_lock = adapter._lock_conversation

    async def paused_lock(connection, requested_user, requested_session):
        result = await original_lock(connection, requested_user, requested_session)
        locked.set()
        await asyncio.Event().wait()
        return result

    adapter._lock_conversation = paused_lock
    task = asyncio.create_task(_append(adapter, session_id, 1))
    await asyncio.wait_for(locked.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await adapter.read_recent(USER_ID, session_id, 10) == ()
    adapter._lock_conversation = original_lock
    async with asyncio.timeout(5):
        assert await _append(adapter, session_id, 2)
    assert len(await adapter.read_recent(USER_ID, session_id, 10)) == 2
