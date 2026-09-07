import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.errors.conversation import ConversationStoreConfigurationError
from app.domain.errors.memory import LongTermMemoryTimeoutError
from app.domain.errors.query_rewriter import QueryRewriterConfigurationError
from app.domain.models.memory import MemoryLifecycleEvent, MemoryProcessResult
from app.infrastructure.postgres.managed_store import ManagedPostgresConversationStore
from app.presentation.api.main import create_app
from tests.integration.test_gateway_api import FakeKiraClient, kira_event, make_settings
from tests.support.context_fakes import FakeRewriter, MemoryStore, pair


class FakeLongTermMemory:
    def __init__(self, result=None):
        self.result = result or MemoryProcessResult()
        self.sources = []

    async def search(self, user_id, query, *, top_k, threshold):
        raise AssertionError("B3 formation must not perform retrieval")

    async def process_memory(self, source):
        self.sources.append(source)
        return self.result


async def test_gateway_preserves_sse_on_write_failure_and_exposes_safe_metrics():
    app = create_app(
        settings=make_settings(),
        kira_client=FakeKiraClient(events=[kira_event('{"text":"answer"}', "answer")]),
        conversation_store=MemoryStore(pair(), write_error=RuntimeError("private payload")),
        query_rewriter=FakeRewriter(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.post(
            "/chat", json={"session_id": "session-1", "message": "followup"}
        )
        metrics = await client.get("/metrics")
        ready = await client.get("/ready")
        rejected = await client.post(
            "/chat",
            json={
                "session_id": "session-1",
                "message": "followup",
                "turn_id": "untrusted",
            },
        )
    assert response.content == b'data: {"text":"answer"}\n\n'
    assert ready.status_code == 200
    assert rejected.status_code == 422
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    assert 'kira_conversation_write_total{outcome="error"} 1.0' in metrics.text
    assert 'kira_context_rewrite_total{outcome="success"} 1.0' in metrics.text
    assert "session-1" not in metrics.text
    assert "private" not in metrics.text


async def test_runtime_schema_error_degrades_chat_but_not_ready_until_revalidated():
    adapter = AsyncMock()
    adapter.read_recent.side_effect = ConversationStoreConfigurationError()
    store = ManagedPostgresConversationStore(adapter, 1)
    await store.validate_schema()
    app = create_app(
        settings=make_settings(),
        kira_client=FakeKiraClient(),
        conversation_store=store,
        query_rewriter=FakeRewriter(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        assert (await client.get("/ready")).status_code == 200
        assert (
            await client.post(
                "/chat",
                json={
                    "session_id": "session-1",
                    "message": "current",
                },
            )
        ).status_code == 200
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/health")).status_code == 200
        await store.validate_schema()
        assert (await client.get("/ready")).status_code == 200


async def test_missing_rewriter_config_fails_wiring_and_closes_owned_http_clients(monkeypatch):
    clients = []
    real_client = httpx.AsyncClient

    def tracked_client():
        client = real_client()
        clients.append(client)
        return client

    monkeypatch.setattr("app.presentation.api.main.httpx.AsyncClient", tracked_client)
    app = create_app(
        settings=make_settings().model_copy(update={"vllm_model": None}),
        conversation_store=MemoryStore(),
    )
    with pytest.raises(QueryRewriterConfigurationError):
        async with app.router.lifespan_context(app):
            pass
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


async def test_injected_http_clients_remain_caller_owned_and_are_separate():
    async with httpx.AsyncClient() as kira_http, httpx.AsyncClient() as rewrite_http:
        app = create_app(
            settings=make_settings(),
            http_client=kira_http,
            rewriter_http_client=rewrite_http,
            conversation_store=MemoryStore(),
        )
        async with app.router.lifespan_context(app):
            assert app.state.query_rewriter is not None
        assert not kira_http.is_closed and not rewrite_http.is_closed


async def test_completed_turn_runs_background_formation_from_persisted_boundary():
    store = MemoryStore(pair())
    memory = FakeLongTermMemory(
        MemoryProcessResult((MemoryLifecycleEvent("ADD", "memory-1", "durable fact"),))
    )
    app = create_app(
        settings=make_settings(),
        kira_client=FakeKiraClient(events=[kira_event('{"text":"answer"}', "exact answer")]),
        conversation_store=store,
        query_rewriter=FakeRewriter(),
        long_term_memory=memory,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.post(
            "/chat", json={"session_id": "session-1", "message": "new question"}
        )
        await app.state.memory_formation_runner.wait_idle()
        metrics = await client.get("/metrics")

    assert response.content == b'data: {"text":"answer"}\n\n'
    assert len(memory.sources) == 1
    source = memory.sources[0]
    assert source.reference.user_id == "test-user"
    assert source.messages[-2].content == "new question"
    assert source.messages[-1].content == "exact answer"
    assert source.messages[-1].turn_id == source.reference.turn_id
    assert 'kira_memory_formation_total{outcome="processed"} 1.0' in metrics.text


async def test_ltm_disabled_does_not_construct_a_formation_runner():
    app = create_app(
        settings=make_settings(),
        kira_client=FakeKiraClient(),
        conversation_store=MemoryStore(),
        query_rewriter=FakeRewriter(),
    )

    async with app.router.lifespan_context(app):
        assert app.state.long_term_memory is None
        assert app.state.memory_formation_runner is None


async def test_memory_formation_never_blocks_the_sse_response_and_cancels_on_shutdown():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingMemory(FakeLongTermMemory):
        async def process_memory(self, source):
            self.sources.append(source)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    memory = HangingMemory()
    app = create_app(
        settings=make_settings().model_copy(update={"memory_operation_timeout_seconds": 0.01}),
        kira_client=FakeKiraClient(events=[kira_event('{"text":"answer"}', "answer")]),
        conversation_store=MemoryStore(),
        query_rewriter=FakeRewriter(),
        long_term_memory=memory,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await asyncio.wait_for(
                client.post("/chat", json={"session_id": "session-1", "message": "question"}),
                timeout=1,
            )
            await asyncio.wait_for(started.wait(), timeout=1)

        assert response.content == b'data: {"text":"answer"}\n\n'
        assert cancelled.is_set() is False

    assert cancelled.is_set()


async def test_memory_runtime_failure_keeps_kira_answer_and_ready_state():
    class FailingMemory(FakeLongTermMemory):
        async def process_memory(self, source):
            raise LongTermMemoryTimeoutError

    app = create_app(
        settings=make_settings(),
        kira_client=FakeKiraClient(events=[kira_event('{"text":"answer"}', "answer")]),
        conversation_store=MemoryStore(),
        query_rewriter=FakeRewriter(),
        long_term_memory=FailingMemory(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.post(
            "/chat", json={"session_id": "session-1", "message": "question"}
        )
        await app.state.memory_formation_runner.wait_idle()
        ready = await client.get("/ready")
        metrics = await client.get("/metrics")

    assert response.content == b'data: {"text":"answer"}\n\n'
    assert b"gateway_error" not in response.content
    assert ready.status_code == 200
    assert (
        'kira_context_degraded_total{dependency="mem0",operation="memory_formation"} 1.0'
        in metrics.text
    )


async def test_enabled_ltm_owns_and_closes_mem0_adapter(monkeypatch):
    class OwnedMemory(FakeLongTermMemory):
        def __init__(self):
            super().__init__()
            self.closed = False

        def close(self):
            self.closed = True

    memory = OwnedMemory()
    monkeypatch.setattr(
        "app.presentation.api.main.Mem0Adapter.from_settings",
        lambda settings: memory,
    )
    settings = make_settings().model_copy(
        update={
            "ltm_enabled": True,
            "memory_database_url": "postgresql://user:secret@db/memory",
            "memory_embedding_base_url": "http://embedding.test",
            "memory_embedding_model": "embedding-model",
            "memory_embedding_dims": 4,
            "memory_llm_base_url": "http://memory-llm.test",
            "memory_llm_model": "memory-model",
        }
    )
    app = create_app(
        settings=settings,
        kira_client=FakeKiraClient(),
        conversation_store=MemoryStore(),
        query_rewriter=FakeRewriter(),
    )

    async with app.router.lifespan_context(app):
        assert app.state.long_term_memory is memory
        assert app.state.memory_formation_runner is not None
        assert memory.closed is False

    assert memory.closed is True
