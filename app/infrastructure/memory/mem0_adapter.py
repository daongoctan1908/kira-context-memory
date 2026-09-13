"""Mem0 adapter behind KiRa's framework-free long-term-memory port."""

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS
from app.application.services.memory_temporal import build_memory_extraction_prompt
from app.config.runtime_contracts import MemoryRuntimeSettings
from app.domain.errors.memory import (
    LongTermMemoryConfigurationError,
    LongTermMemoryConnectionError,
    LongTermMemoryOperationError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.models.memory import (
    LongTermMemory,
    MemoryLifecycleEvent,
    MemoryProcessResult,
    MemorySource,
)
from app.infrastructure.memory.postgres_admin import normalize_psycopg_dsn


class AsyncMem0Client(Protocol):
    async def search(self, query: str, **kwargs: Any) -> object: ...

    async def add(self, messages: object, **kwargs: Any) -> object: ...

    def close(self) -> None: ...


def _openai_base_url(value: object) -> str:
    normalized = str(value).rstrip("/")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def build_mem0_config(settings: MemoryRuntimeSettings) -> dict[str, object]:
    """Build the internal-package config without leaking credentials to core or logs."""
    if (
        settings.memory_database_url is None
        or settings.memory_embedding_base_url is None
        or settings.memory_embedding_model is None
        or settings.memory_embedding_dims is None
        or settings.memory_llm_base_url is None
        or settings.memory_llm_model is None
    ):
        raise LongTermMemoryConfigurationError

    database_url = normalize_psycopg_dsn(str(settings.memory_database_url.get_secret_value()))
    embedding_api_key = (
        settings.memory_embedding_api_key.get_secret_value()
        if settings.memory_embedding_api_key is not None
        else "not-required"
    )
    llm_api_key = (
        settings.memory_llm_api_key.get_secret_value()
        if settings.memory_llm_api_key is not None
        else "not-required"
    )
    return {
        "version": "v1.1",
        "history_db_path": ":memory:",
        "custom_instructions": MEMORY_EXTRACTION_INSTRUCTIONS,
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "connection_string": database_url,
                "collection_name": settings.memory_collection_name,
                "schema_name": settings.memory_schema,
                "auto_create": False,
                "embedding_model_dims": settings.memory_embedding_dims,
                "hnsw": True,
                "diskann": False,
                "minconn": settings.memory_postgres_min_connections,
                "maxconn": settings.memory_postgres_max_connections,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": settings.memory_embedding_model,
                "api_key": embedding_api_key,
                "openai_base_url": _openai_base_url(settings.memory_embedding_base_url),
                "embedding_dims": settings.memory_embedding_dims,
            },
        },
        "llm": {
            "provider": "vllm",
            "config": {
                "model": settings.memory_llm_model,
                "api_key": llm_api_key,
                "vllm_base_url": _openai_base_url(settings.memory_llm_base_url),
                "temperature": settings.memory_llm_temperature,
                "max_tokens": settings.memory_llm_max_tokens,
            },
        },
    }


def create_mem0_client(settings: MemoryRuntimeSettings) -> AsyncMem0Client:
    """Construct the vendored client with telemetry disabled and runtime DDL forbidden."""
    os.environ["MEM0_TELEMETRY"] = "False"
    mem0_logger = logging.getLogger("mem0")
    if not any(isinstance(handler, logging.NullHandler) for handler in mem0_logger.handlers):
        mem0_logger.addHandler(logging.NullHandler())
    mem0_logger.setLevel(logging.CRITICAL)
    mem0_logger.propagate = False
    try:
        from mem0 import AsyncMemory

        return AsyncMemory.from_config(build_mem0_config(settings))
    except LongTermMemoryConfigurationError:
        raise
    except Exception as error:
        raise LongTermMemoryConfigurationError from error


class Mem0Adapter:
    """User-scoped search and lifecycle-neutral Mem0 formation."""

    def __init__(
        self,
        client: AsyncMem0Client,
        *,
        search_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> None:
        self._client = client
        self._search_timeout = search_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    @classmethod
    def from_settings(cls, settings: MemoryRuntimeSettings) -> "Mem0Adapter":
        return cls(
            create_mem0_client(settings),
            search_timeout_seconds=settings.memory_search_timeout_seconds,
            operation_timeout_seconds=settings.memory_operation_timeout_seconds,
        )

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        if not user_id.strip() or not query.strip():
            raise ValueError("user_id and query must not be empty")
        try:
            async with asyncio.timeout(self._search_timeout):
                response = await self._client.search(
                    query,
                    top_k=top_k,
                    threshold=threshold,
                    filters={"user_id": user_id},
                    rerank=False,
                )
        except TimeoutError as error:
            raise LongTermMemoryTimeoutError from error
        except Exception as error:
            self._raise_mapped(error)

        try:
            if not isinstance(response, Mapping):
                raise ValueError
            rows = response["results"]
            if not isinstance(rows, list):
                raise ValueError
            return tuple(self._parse_memory(row, user_id) for row in rows)
        except (KeyError, TypeError, ValueError) as error:
            raise LongTermMemoryProtocolError from error

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        messages = [
            {"role": message.role.value, "content": message.content} for message in source.messages
        ]
        reference = source.reference
        try:
            async with asyncio.timeout(self._operation_timeout):
                response = await self._client.add(
                    messages,
                    user_id=reference.user_id,
                    metadata={
                        "formation_event_id": str(source.formation_event_id),
                        "conversation_id": str(reference.conversation_id),
                        "turn_id": reference.turn_id,
                        "boundary_message_id": reference.boundary_message_id,
                    },
                    infer=True,
                    prompt=build_memory_extraction_prompt(source.messages),
                )
        except TimeoutError as error:
            raise LongTermMemoryTimeoutError from error
        except Exception as error:
            self._raise_mapped(error)

        try:
            if not isinstance(response, Mapping):
                raise ValueError
            rows = response["results"]
            if not isinstance(rows, list):
                raise ValueError
            return MemoryProcessResult(tuple(self._parse_lifecycle_event(row) for row in rows))
        except (KeyError, TypeError, ValueError) as error:
            raise LongTermMemoryProtocolError from error

    def close(self) -> None:
        stores = {
            id(store): store
            for store in (
                getattr(self._client, "vector_store", None),
                getattr(self._client, "_entity_store", None),
            )
            if store is not None
        }
        try:
            self._client.close()
        finally:
            for store in stores.values():
                close = getattr(store, "close", None)
                if close is not None:
                    close()

    @staticmethod
    def _parse_memory(row: object, user_id: str) -> LongTermMemory:
        if not isinstance(row, Mapping):
            raise ValueError
        metadata_value = row.get("metadata", {})
        if not isinstance(metadata_value, Mapping):
            raise ValueError
        owner = row.get("user_id", metadata_value.get("user_id"))
        if owner != user_id:
            raise ValueError
        memory_id = row.get("id")
        content = row.get("memory")
        score = row.get("score")
        if (
            not isinstance(memory_id, str)
            or not isinstance(content, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
        ):
            raise ValueError
        return LongTermMemory(
            memory_id=memory_id,
            content=content,
            score=float(score),
            metadata=dict(metadata_value),
        )

    @staticmethod
    def _parse_lifecycle_event(row: object) -> MemoryLifecycleEvent:
        if not isinstance(row, Mapping):
            raise ValueError
        action = row.get("event")
        memory_id = row.get("id")
        content = row.get("memory")
        if not isinstance(action, str) or not action.strip():
            raise ValueError
        if memory_id is not None and not isinstance(memory_id, str):
            raise ValueError
        if content is not None and not isinstance(content, str):
            raise ValueError
        return MemoryLifecycleEvent(
            action=action,
            memory_id=memory_id,
            content=content,
        )

    @staticmethod
    def _raise_mapped(error: Exception) -> None:
        if (
            isinstance(error, (httpx.TimeoutException, TimeoutError))
            or "Timeout" in type(error).__name__
        ):
            raise LongTermMemoryTimeoutError from error
        if (
            isinstance(error, (httpx.RequestError, ConnectionError, OSError))
            or "Connection" in type(error).__name__
        ):
            raise LongTermMemoryConnectionError from error
        raise LongTermMemoryOperationError from error
