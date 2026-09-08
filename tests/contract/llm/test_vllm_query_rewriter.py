import asyncio
import json

import httpx
import pytest

from app.application.services.context_builder import ContextBuilder
from app.application.services.rewrite_prompt import build_rewrite_messages
from app.config.settings import Settings
from app.domain.errors.query_rewriter import (
    QueryRewriterConfigurationError,
    QueryRewriterConnectionError,
    QueryRewriterHttpError,
    QueryRewriterProtocolError,
    QueryRewriterTimeoutError,
)
from app.domain.models.memory import LongTermMemory
from app.infrastructure.llm.vllm_query_rewriter import VllmQueryRewriterAdapter


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "kira_base_url": "http://kira.test:8122",
        "kira_username": "service-account",
        "kira_basic_auth": "fake-basic-secret",
        "vllm_base_url": "http://vllm.test:8000",
        "vllm_model": "configured-test-model",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def completion(content: object, *, finish_reason: str = "stop") -> dict[str, object]:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


@pytest.mark.parametrize("api_key", [None, "fake-vllm-secret"])
@pytest.mark.parametrize("base_path", ["", "/", "/v1", "/v1/"])
async def test_request_contract_model_auth_timeouts_and_plain_text_output(
    api_key: str | None,
    base_path: str,
) -> None:
    requests: list[httpx.Request] = []
    context = ContextBuilder().build([], "Doanh thu Hưng Yên?")

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("  Doanh thu Hưng Yên? \n"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = VllmQueryRewriterAdapter(
            client,
            make_settings(vllm_api_key=api_key, vllm_base_url="http://vllm.test:8000" + base_path),
        )
        result = await port.rewrite(context)
        assert client.is_closed is False

    assert result == "Doanh thu Hưng Yên?"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://vllm.test:8000/v1/chat/completions"
    assert request.headers["content-type"] == "application/json"
    if api_key:
        assert request.headers["authorization"] == f"Bearer {api_key}"
    else:
        assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "model": "configured-test-model",
        "messages": build_rewrite_messages(context),
        "temperature": 0,
        "stream": False,
        "max_tokens": 256,
    }
    assert request.extensions["timeout"] == {"connect": 2.0, "read": 8.0, "write": 2.0, "pool": 2.0}


async def test_custom_timeouts_output_limit_and_model_are_honored() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["connect"] == 1.0
        assert request.extensions["timeout"]["read"] == 3.0
        assert json.loads(request.content)["model"] == "another-model"
        return httpx.Response(200, json=completion("abcd"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = VllmQueryRewriterAdapter(
            client,
            make_settings(
                vllm_connect_timeout_seconds=1,
                vllm_read_timeout_seconds=3,
                vllm_max_output_chars=4,
                vllm_model=" another-model ",
            ),
        )
        assert await adapter.rewrite(ContextBuilder().build([], "q")) == "abcd"


async def test_request_contains_ranked_ltm_content_without_provider_metadata() -> None:
    requests: list[httpx.Request] = []
    memories = (
        LongTermMemory("private-id-1", "fact ranked first", 0.9, {"private": "one"}),
        LongTermMemory("private-id-2", "fact ranked second", 0.8, {"private": "two"}),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("standalone query"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = VllmQueryRewriterAdapter(client, make_settings())
        result = await adapter.rewrite(ContextBuilder().build([], "follow-up", memories))

    assert result == "standalone query"
    prompt_envelope = json.loads(json.loads(requests[0].content)["messages"][1]["content"])
    assert prompt_envelope["long_term_memories"] == [
        "fact ranked first",
        "fact ranked second",
    ]
    serialized_request = requests[0].content.decode()
    assert "private-id" not in serialized_request
    assert '"score"' not in serialized_request
    assert '"metadata"' not in serialized_request


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500, 503])
async def test_non_success_is_typed_without_retry_redirect_or_response_body_leak(
    status: int,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            text="private downstream body",
            headers={"Location": "http://other.test/"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        adapter = VllmQueryRewriterAdapter(client, make_settings())
        with pytest.raises(QueryRewriterHttpError) as error:
            await adapter.rewrite(ContextBuilder().build([], "private query"))

    assert calls == 1
    assert error.value.status_code == status
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (httpx.ConnectTimeout, QueryRewriterTimeoutError),
        (httpx.ReadTimeout, QueryRewriterTimeoutError),
        (httpx.WriteTimeout, QueryRewriterTimeoutError),
        (httpx.PoolTimeout, QueryRewriterTimeoutError),
        (httpx.ConnectError, QueryRewriterConnectionError),
        (httpx.ReadError, QueryRewriterConnectionError),
        (httpx.RemoteProtocolError, QueryRewriterConnectionError),
    ],
)
async def test_transport_failure_is_typed_and_sanitized_without_retry(failure, expected) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise failure("private transport details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(expected) as error:
            await VllmQueryRewriterAdapter(client, make_settings()).rewrite(
                ContextBuilder().build([], "private query")
            )

    assert calls == 1
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"choices": []},
        {"choices": {}},
        {"choices": [None]},
        {"choices": [{}]},
        {"choices": [{"message": []}]},
        {"choices": [{"message": {}}]},
        completion(None),
        completion([]),
        completion(12),
        completion(""),
        completion(" \n\t "),
        completion("x" * 2049),
        completion("ế" * 2049),
        completion("partial", finish_reason="length"),
        completion("filtered", finish_reason="content_filter"),
        {"choices": [{"message": {"content": "query", "tool_calls": [{"name": "tool"}]}}]},
    ],
)
async def test_malformed_empty_oversized_or_incomplete_output_is_rejected(body: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QueryRewriterProtocolError):
            await VllmQueryRewriterAdapter(client, make_settings()).rewrite(
                ContextBuilder().build([], "q")
            )


async def test_invalid_json_is_rejected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json"))
    ) as client:
        with pytest.raises(QueryRewriterProtocolError):
            await VllmQueryRewriterAdapter(client, make_settings()).rewrite(
                ContextBuilder().build([], "q")
            )


async def test_cancellation_is_not_swallowed_or_converted_to_dependency_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await VllmQueryRewriterAdapter(client, make_settings()).rewrite(
                ContextBuilder().build([], "q")
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"vllm_base_url": None},
        {"vllm_model": None},
        {"vllm_model": "   "},
        {"vllm_api_key": "   "},
        {"vllm_base_url": "http://user:password@vllm.test"},
        {"vllm_base_url": "http://vllm.test?key=secret"},
        {"vllm_base_url": "http://vllm.test#fragment"},
    ],
)
async def test_adapter_requires_explicit_valid_configuration(overrides: dict[str, object]) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(QueryRewriterConfigurationError):
            VllmQueryRewriterAdapter(client, make_settings(**overrides))
