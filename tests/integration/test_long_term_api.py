import httpx
import pytest

from app.config.settings import Settings
from app.domain.errors.memory import (
    LongTermMemoryConfigurationError,
    LongTermMemoryConnectionError,
)
from app.domain.models.memory import LongTermMemory
from app.infrastructure.memory import Mem0Adapter
from app.presentation.api.main import create_app
from tests.integration.test_gateway_api import FakeKiraClient, kira_event
from tests.support.context_fakes import FakeRewriter, MemoryStore, pair


def enabled_settings() -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="http://kira.test:8122",
        kira_username="service-account",
        kira_basic_auth="basic-credential",
        vllm_base_url="http://rewriter.test",
        vllm_model="test-model",
        app_environment="test",
        dev_static_identity_enabled=True,
        dev_static_user_id="trusted-user",
        ltm_enabled=True,
        memory_database_url="postgresql://memory:secret@postgres.test/memory",
        memory_embedding_base_url="http://embedding.test:8000",
        memory_embedding_model="embedding-model",
        memory_embedding_dims=3,
        memory_llm_base_url="http://memory-llm.test:8000",
        memory_llm_model="memory-model",
    )


class FakeLongTermMemory:
    def __init__(self, memories=(), *, error=None):
        self.memories = tuple(memories)
        self.error = error
        self.searches = []
        self.close_calls = 0

    async def search(self, user_id, query, *, top_k, threshold):
        self.searches.append((user_id, query, top_k, threshold))
        if self.error is not None:
            raise self.error
        return self.memories

    def close(self):
        self.close_calls += 1


def ranked_memory() -> LongTermMemory:
    return LongTermMemory(
        "provider-private-id",
        "User explicitly prefers province comparisons.",
        0.9,
        {"private": "provider-metadata"},
    )


async def test_disabled_ltm_does_not_construct_adapter(monkeypatch):
    calls = 0

    def unexpected_factory(cls, settings):
        nonlocal calls
        calls += 1
        raise AssertionError("disabled LTM must not construct Mem0")

    monkeypatch.setattr(Mem0Adapter, "from_settings", classmethod(unexpected_factory))
    settings = enabled_settings().model_copy(update={"ltm_enabled": False})
    app = create_app(
        settings=settings,
        kira_client=FakeKiraClient(),
        conversation_store=MemoryStore(),
        query_rewriter=FakeRewriter(),
    )

    async with app.router.lifespan_context(app):
        assert app.state.long_term_memory is None
        assert app.state.ltm_status == "disabled"
        assert app.state.ready is True

    assert calls == 0


async def test_enabled_ltm_constructs_and_closes_one_owned_adapter(monkeypatch):
    memory = FakeLongTermMemory()
    calls = []

    def factory(cls, settings):
        calls.append(settings)
        return memory

    monkeypatch.setattr(Mem0Adapter, "from_settings", classmethod(factory))
    settings = enabled_settings()
    app = create_app(
        settings=settings,
        kira_client=FakeKiraClient(),
        conversation_store=MemoryStore(),
        query_rewriter=FakeRewriter(),
    )

    async with app.router.lifespan_context(app):
        assert app.state.long_term_memory is memory
        assert app.state.ltm_status == "available"
        assert memory.close_calls == 0

    assert calls == [settings]
    assert memory.close_calls == 1


async def test_injected_ltm_is_wired_but_remains_caller_owned():
    memory = FakeLongTermMemory((ranked_memory(),))
    rewriter = FakeRewriter("standalone using LTM")
    kira = FakeKiraClient(events=[kira_event('{"text":"answer"}', "answer")])
    app = create_app(
        settings=enabled_settings().model_copy(update={"ltm_enabled": False}),
        kira_client=kira,
        conversation_store=MemoryStore(pair()),
        query_rewriter=rewriter,
        long_term_memory=memory,
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
            json={"session_id": "session-1", "message": "follow-up"},
        )
        metrics = await client.get("/metrics")

        assert app.state.ltm_status == "injected"
        assert response.status_code == 200
        assert kira.messages == ["standalone using LTM"]
        assert rewriter.contexts[0].long_term_memories == (ranked_memory(),)
        assert memory.searches == [("trusted-user", "follow-up", 10, 0.1)]
        assert 'kira_memory_search_total{outcome="success"} 1.0' in metrics.text
        assert "kira_memory_search_results_sum 1.0" in metrics.text
        assert "provider-private" not in metrics.text

    assert memory.close_calls == 0


async def test_runtime_ltm_failure_degrades_without_affecting_sse_or_readiness():
    memory = FakeLongTermMemory(error=LongTermMemoryConnectionError())
    rewriter = FakeRewriter("standalone from recent")
    kira = FakeKiraClient(events=[kira_event('{"text":"answer"}', "answer")])
    app = create_app(
        settings=enabled_settings(),
        kira_client=kira,
        conversation_store=MemoryStore(pair()),
        query_rewriter=rewriter,
        long_term_memory=memory,
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
            json={"session_id": "session-1", "message": "follow-up"},
        )
        ready = await client.get("/ready")
        metrics = await client.get("/metrics")

    assert response.status_code == 200
    assert response.content == b'data: {"text":"answer"}\n\n'
    assert kira.messages == ["standalone from recent"]
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert 'kira_memory_search_total{outcome="error"} 1.0' in metrics.text
    assert (
        'kira_context_degraded_total{dependency="mem0",operation="memory_search"} 1.0'
        in metrics.text
    )
    assert "LongTermMemoryConnectionError" not in metrics.text


async def test_enabled_ltm_wiring_failure_fails_startup_and_closes_owned_clients(monkeypatch):
    clients = []
    real_client = httpx.AsyncClient

    def tracked_client():
        client = real_client()
        clients.append(client)
        return client

    def failing_factory(cls, settings):
        raise LongTermMemoryConfigurationError()

    monkeypatch.setattr("app.presentation.api.main.httpx.AsyncClient", tracked_client)
    monkeypatch.setattr(Mem0Adapter, "from_settings", classmethod(failing_factory))
    app = create_app(
        settings=enabled_settings(),
        conversation_store=MemoryStore(),
    )

    with pytest.raises(LongTermMemoryConfigurationError):
        async with app.router.lifespan_context(app):
            pass

    assert app.state.ready is False
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)
