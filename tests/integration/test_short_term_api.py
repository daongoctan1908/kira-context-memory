from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.errors.conversation import ConversationStoreConfigurationError
from app.domain.errors.query_rewriter import QueryRewriterConfigurationError
from app.infrastructure.postgres.managed_store import ManagedPostgresConversationStore
from app.presentation.api.main import create_app
from tests.integration.test_gateway_api import FakeKiraClient, kira_event, make_settings
from tests.support.context_fakes import FakeRewriter, MemoryStore, pair


async def test_gateway_preserves_sse_on_write_failure_and_exposes_safe_metrics():
    app = create_app(
        settings=make_settings().model_copy(update={"memory_formation_enabled": True}),
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
    assert 'kira_memory_job_schedule_total{outcome="error"} 1.0' in metrics.text
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
