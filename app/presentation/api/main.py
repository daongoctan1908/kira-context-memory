"""FastAPI application factory and dependency lifecycle."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.application.use_cases.handle_chat import HandleChatUseCase
from app.config.settings import Settings, get_settings
from app.domain.errors.conversation import ConversationStoreError
from app.domain.errors.kira import KiraClientError
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.kira_client import KiraClientPort
from app.infrastructure.kira.http_kira_client import KiraHttpAdapter
from app.infrastructure.redis import RedisConversationStoreAdapter, create_redis_client
from app.presentation.api.chat_router import router as chat_router
from app.presentation.api.errors import kira_client_exception_handler
from app.presentation.api.health_router import router as health_router

logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    kira_client: KiraClientPort | None = None,
    http_client: httpx.AsyncClient | None = None,
    conversation_store: ConversationStorePort | None = None,
    redis_client: Redis | None = None,
) -> FastAPI:
    """Create a Gateway app with optional dependency injection for tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or get_settings()
        logging.basicConfig(
            level=resolved_settings.app_log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

        owned_http_client: httpx.AsyncClient | None = None
        owned_redis_client: Redis | None = None
        resolved_kira_client = kira_client
        if resolved_kira_client is None:
            resolved_http_client = http_client
            if resolved_http_client is None:
                owned_http_client = httpx.AsyncClient()
                resolved_http_client = owned_http_client
            resolved_kira_client = KiraHttpAdapter(resolved_http_client, resolved_settings)

        resolved_conversation_store = conversation_store
        redis_status = "injected" if conversation_store is not None else "disabled"
        if resolved_conversation_store is None and (
            redis_client is not None or resolved_settings.redis_url is not None
        ):
            resolved_redis_client = redis_client
            if resolved_redis_client is None:
                owned_redis_client = create_redis_client(resolved_settings)
                resolved_redis_client = owned_redis_client
            redis_adapter = RedisConversationStoreAdapter(
                resolved_redis_client,
                session_ttl_seconds=resolved_settings.redis_session_ttl_seconds,
                max_recent_messages=resolved_settings.max_recent_messages,
            )
            resolved_conversation_store = redis_adapter
            try:
                await redis_adapter.ping()
                redis_status = "available"
            except ConversationStoreError as error:
                redis_status = "degraded"
                logger.warning(
                    "Redis conversation store unavailable during startup",
                    extra={
                        "dependency": "redis",
                        "operation": "ping",
                        "exception_type": type(error).__name__,
                        "degraded_mode": True,
                    },
                )

        application.state.settings = resolved_settings
        application.state.kira_client = resolved_kira_client
        application.state.conversation_store = resolved_conversation_store
        application.state.redis_status = redis_status
        application.state.handle_chat = HandleChatUseCase(resolved_kira_client)
        application.state.ready = True
        try:
            yield
        finally:
            application.state.ready = False
            if owned_http_client is not None:
                await owned_http_client.aclose()
            if owned_redis_client is not None:
                await owned_redis_client.aclose(close_connection_pool=True)

    application = FastAPI(
        title="KiRa Context Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.ready = False
    application.add_exception_handler(KiraClientError, kira_client_exception_handler)
    application.include_router(health_router)
    application.include_router(chat_router)
    return application


app = create_app()
