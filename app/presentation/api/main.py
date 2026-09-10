"""FastAPI application factory and dependency lifecycle."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.services.context_builder import ContextBuilder
from app.application.use_cases.handle_chat import HandleChatUseCase
from app.config.settings import Settings, get_settings
from app.domain.errors.conversation import ConversationStoreConnectionError
from app.domain.errors.kira import KiraClientError
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.identity import IdentityPort
from app.domain.ports.kira_client import KiraClientPort
from app.domain.ports.long_term_memory import LongTermMemoryPort
from app.domain.ports.query_rewriter import QueryRewriterPort
from app.infrastructure.identity import NullIdentityAdapter, StaticIdentityAdapter
from app.infrastructure.kira.http_kira_client import KiraHttpAdapter
from app.infrastructure.llm.vllm_query_rewriter import VllmQueryRewriterAdapter
from app.infrastructure.memory import Mem0Adapter
from app.infrastructure.observability.context import ContextTelemetry, configure_app_logging
from app.infrastructure.postgres import (
    PostgresConversationStoreAdapter,
    create_postgres_engine,
)
from app.infrastructure.postgres.managed_store import ManagedPostgresConversationStore
from app.presentation.api.chat_router import router as chat_router
from app.presentation.api.errors import kira_client_exception_handler
from app.presentation.api.health_router import router as health_router
from app.presentation.api.metrics_router import router as metrics_router

logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    kira_client: KiraClientPort | None = None,
    http_client: httpx.AsyncClient | None = None,
    conversation_store: ConversationStorePort | None = None,
    postgres_engine: AsyncEngine | None = None,
    query_rewriter: QueryRewriterPort | None = None,
    rewriter_http_client: httpx.AsyncClient | None = None,
    identity_provider: IdentityPort | None = None,
    long_term_memory: LongTermMemoryPort | None = None,
) -> FastAPI:
    """Create a Gateway app with optional dependency injection for tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or get_settings()
        configure_app_logging(resolved_settings.app_log_level)
        telemetry = ContextTelemetry()

        owned_http_client: httpx.AsyncClient | None = None
        owned_rewriter_http_client: httpx.AsyncClient | None = None
        owned_postgres_engine: AsyncEngine | None = None
        owned_long_term_memory: Mem0Adapter | None = None
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
                managed_store = ManagedPostgresConversationStore(
                    postgres_adapter,
                    resolved_settings.conversation_operation_timeout_seconds,
                )
                resolved_conversation_store = managed_store
                try:
                    await managed_store.validate_schema()
                    postgres_status = "available"
                except ConversationStoreConnectionError as error:
                    postgres_status = "degraded"
                    logger.warning(
                        "PostgreSQL conversation store unavailable during startup",
                        extra={
                            "dependency": "postgresql",
                            "operation": "validate_schema",
                            "error_class": type(error).__name__,
                            "fallback_mode": "original_query",
                        },
                    )

            resolved_rewriter = query_rewriter
            if resolved_rewriter is None:
                resolved_rewriter_http = rewriter_http_client
                if resolved_rewriter_http is None:
                    # Separate pool: never inherit KiRa headers, cookies or credentials.
                    owned_rewriter_http_client = httpx.AsyncClient()
                    resolved_rewriter_http = owned_rewriter_http_client
                resolved_rewriter = VllmQueryRewriterAdapter(
                    resolved_rewriter_http,
                    resolved_settings,
                )

            resolved_identity = identity_provider
            if resolved_identity is None:
                if resolved_settings.dev_static_identity_enabled:
                    resolved_identity = StaticIdentityAdapter(
                        resolved_settings.dev_static_user_id or ""
                    )
                else:
                    resolved_identity = NullIdentityAdapter()

            resolved_long_term_memory = long_term_memory
            if resolved_long_term_memory is not None:
                ltm_status = "injected"
            elif resolved_settings.ltm_enabled:
                owned_long_term_memory = Mem0Adapter.from_settings(resolved_settings)
                resolved_long_term_memory = owned_long_term_memory
                ltm_status = "available"
            else:
                ltm_status = "disabled"

            application.state.settings = resolved_settings
            application.state.kira_client = resolved_kira_client
            application.state.conversation_store = resolved_conversation_store
            application.state.postgres_status = postgres_status
            application.state.query_rewriter = resolved_rewriter
            application.state.long_term_memory = resolved_long_term_memory
            application.state.ltm_status = ltm_status
            application.state.memory_formation_enabled = resolved_settings.memory_formation_enabled
            application.state.telemetry = telemetry
            application.state.identity_provider = resolved_identity
            application.state.handle_chat = HandleChatUseCase(
                resolved_kira_client,
                conversation_store=resolved_conversation_store,
                query_rewriter=resolved_rewriter,
                context_builder=ContextBuilder(
                    max_recent_messages=resolved_settings.max_recent_messages,
                    recent_token_budget=resolved_settings.recent_context_token_budget,
                    max_long_term_memories=resolved_settings.memory_search_top_k,
                ),
                observer=telemetry,
                max_recent_messages=resolved_settings.max_recent_messages,
                store_timeout_seconds=resolved_settings.conversation_operation_timeout_seconds,
                long_term_memory=resolved_long_term_memory,
                memory_search_top_k=resolved_settings.memory_search_top_k,
                memory_search_threshold=resolved_settings.memory_search_threshold,
                memory_search_timeout_seconds=resolved_settings.memory_search_timeout_seconds,
                memory_formation_enabled=resolved_settings.memory_formation_enabled,
            )
            application.state.ready = True
            yield
        finally:
            application.state.ready = False
            try:
                if owned_long_term_memory is not None:
                    owned_long_term_memory.close()
            finally:
                try:
                    if owned_http_client is not None:
                        await owned_http_client.aclose()
                finally:
                    try:
                        if owned_rewriter_http_client is not None:
                            await owned_rewriter_http_client.aclose()
                    finally:
                        if owned_postgres_engine is not None:
                            await owned_postgres_engine.dispose()

    application = FastAPI(
        title="KiRa Context Gateway",
        version="0.4.1",
        lifespan=lifespan,
    )
    application.state.ready = False
    application.add_exception_handler(KiraClientError, kira_client_exception_handler)
    application.include_router(health_router)
    application.include_router(chat_router)
    application.include_router(metrics_router)
    return application


app = create_app()
