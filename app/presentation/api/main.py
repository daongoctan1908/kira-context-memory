"""FastAPI application factory and dependency lifecycle."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.use_cases.handle_chat import HandleChatUseCase
from app.config.settings import Settings, get_settings
from app.domain.errors.conversation import ConversationStoreConnectionError
from app.domain.errors.kira import KiraClientError
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.kira_client import KiraClientPort
from app.infrastructure.kira.http_kira_client import KiraHttpAdapter
from app.infrastructure.postgres import (
    PostgresConversationStoreAdapter,
    create_postgres_engine,
)
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
    postgres_engine: AsyncEngine | None = None,
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
        owned_postgres_engine: AsyncEngine | None = None
        try:
            resolved_kira_client = kira_client
            if resolved_kira_client is None:
                resolved_http_client = http_client
                if resolved_http_client is None:
                    owned_http_client = httpx.AsyncClient()
                    resolved_http_client = owned_http_client
                resolved_kira_client = KiraHttpAdapter(resolved_http_client, resolved_settings)

            resolved_conversation_store = conversation_store
            postgres_status = "injected" if conversation_store is not None else "initializing"
            if resolved_conversation_store is None:
                resolved_postgres_engine = postgres_engine
                if resolved_postgres_engine is None:
                    owned_postgres_engine = create_postgres_engine(resolved_settings)
                    resolved_postgres_engine = owned_postgres_engine
                postgres_adapter = PostgresConversationStoreAdapter(resolved_postgres_engine)
                resolved_conversation_store = postgres_adapter
                try:
                    await postgres_adapter.validate_schema()
                    postgres_status = "available"
                except ConversationStoreConnectionError as error:
                    postgres_status = "degraded"
                    logger.warning(
                        "PostgreSQL conversation store unavailable during startup",
                        extra={
                            "dependency": "postgresql",
                            "operation": "validate_schema",
                            "exception_type": type(error).__name__,
                            "degraded_mode": True,
                        },
                    )

            application.state.settings = resolved_settings
            application.state.kira_client = resolved_kira_client
            application.state.conversation_store = resolved_conversation_store
            application.state.postgres_status = postgres_status
            application.state.handle_chat = HandleChatUseCase(resolved_kira_client)
            application.state.ready = True
            yield
        finally:
            application.state.ready = False
            if owned_http_client is not None:
                await owned_http_client.aclose()
            if owned_postgres_engine is not None:
                await owned_postgres_engine.dispose()

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
