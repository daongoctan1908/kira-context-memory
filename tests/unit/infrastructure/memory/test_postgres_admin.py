import httpx
import pytest

from app.config.settings import Settings
from app.domain.errors.memory import (
    LongTermMemoryConfigurationError,
    LongTermMemoryProtocolError,
)
from app.infrastructure.memory.postgres_admin import (
    embeddings_url,
    initialize_memory_schema,
    normalize_psycopg_dsn,
    probe_embedding_dimension,
)


def settings(**updates) -> Settings:
    values = {
        "_env_file": None,
        "kira_base_url": "http://kira.test",
        "kira_username": "service-account",
        "kira_basic_auth": "credential",
        "memory_database_url": "postgresql+asyncpg://user:secret@db/memory",
        "memory_embedding_base_url": "http://embed.test",
        "memory_embedding_model": "embed-model",
        "memory_embedding_api_key": "secret",
        "memory_embedding_dims": 3,
    }
    values.update(updates)
    return Settings(**values)


def test_url_and_dsn_normalization():
    assert embeddings_url("http://embed.test") == "http://embed.test/v1/embeddings"
    assert embeddings_url("http://embed.test/v1/") == "http://embed.test/v1/embeddings"
    assert normalize_psycopg_dsn("postgresql+asyncpg://user@db/name") == "postgresql://user@db/name"
    with pytest.raises(LongTermMemoryConfigurationError):
        normalize_psycopg_dsn("sqlite:///memory.db")


async def test_embedding_probe_validates_auth_contract_and_dimension():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await probe_embedding_dimension(settings(), client) == 3

    request = seen["request"]
    assert request.url == "http://embed.test/v1/embeddings"
    assert request.headers["authorization"] == "Bearer secret"
    assert b'"model":"embed-model"' in request.content
    assert b'"dimensions"' not in request.content


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"data": [{"embedding": [0.1]}]}, LongTermMemoryConfigurationError),
        ({"data": [{"embedding": [True, 0.2, 0.3]}]}, LongTermMemoryProtocolError),
        ({"unexpected": []}, LongTermMemoryProtocolError),
    ],
)
async def test_embedding_probe_rejects_mismatch_or_malformed_payload(payload, error):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error):
            await probe_embedding_dimension(settings(), client)


async def test_initializer_uses_admin_dsn_after_successful_probe(monkeypatch):
    captured = {}

    def initialize_sync(*args):
        captured["args"] = args
        return "ready"

    monkeypatch.setattr(
        "app.infrastructure.memory.postgres_admin._initialize_memory_schema_sync",
        initialize_sync,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    configured = settings(memory_admin_database_url="postgresql://admin:secret@db/memory")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await initialize_memory_schema(configured, client) == "ready"

    assert captured["args"] == (
        "postgresql://admin:secret@db/memory",
        "memory",
        "memories",
        "embed-model",
        3,
    )
