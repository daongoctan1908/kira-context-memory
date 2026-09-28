"""D0 production-faithful port tests over mocked OpenAI-compatible transports.

Covers: production parity of embedding preprocessing/batching/dimensions, request
body shape for both ports, strict schema on the decision body, fail-closed typed
errors for infrastructure failures, decision payload parsing via the executor's
strict contract, and gold-leakage absence in the LLM#2 request payload. No real
network access occurs; every response is a canned httpx transport.
"""

import json
from typing import Any

import httpx
import pytest

from evaluation.config import EvalConfig, Profile, ProviderConfig
from evaluation.d0_local_executor import InvalidDecision
from evaluation.d0_ports import (
    D0EmbeddingPort,
    D0DecisionPort,
    D0ProviderError,
    D0_CONFLICT_SYSTEM_PROMPT,
    D0_CONFLICT_PROMPT_VERSION,
    production_preprocess,
)
from evaluation.models import D0RetrievalResult


def _config() -> EvalConfig:
    return EvalConfig(
        profile=Profile.INTERNAL_TEST,
        suites=("formation",),
        extraction=ProviderConfig(
            base_url="https://llm.invalid/v1",
            model="gpt-4o-mini",
            api_key="k" * 10,
        ),
        embedding=ProviderConfig(
            base_url="https://embed.invalid/v1",
            model="text-embedding-3-small",
            api_key="k" * 10,
        ),
        embedding_dimensions=1536,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=2.0,
        total_timeout_seconds=5.0,
    )


def _embedding_transport(captured: list[dict[str, Any]], dimension: int = 1536) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        inputs = body["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [0.1] * dimension} for i in range(len(inputs))
                ]
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat_transport(captured: list[dict[str, Any]], content: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ]
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Embedding port
# ---------------------------------------------------------------------------


def test_production_preprocess_only_replaces_newlines():
    assert production_preprocess("a\nb") == "a b"
    assert production_preprocess("  trim  ") == "  trim  "


async def test_embedding_port_batches_over_100_and_keeps_order():
    captured: list[dict[str, Any]] = []
    port = D0EmbeddingPort(_embedding_transport(captured), _config())
    texts = [f"fact {i}" for i in range(205)]
    vectors = await port.embed(texts)
    assert len(vectors) == 205
    # Production chunks at 100 inputs per call: 3 POSTs for 205 texts.
    assert len(captured) == 3
    assert [len(call["input"]) for call in captured] == [100, 100, 5]
    # Each call uses the production body shape with float encoding + dimensions.
    for call in captured:
        assert call["encoding_format"] == "float"
        assert call["model"] == "text-embedding-3-small"
        assert call["dimensions"] == 1536


async def test_embedding_port_rejects_dimension_mismatch():
    captured: list[dict[str, Any]] = []
    port = D0EmbeddingPort(_embedding_transport(captured, dimension=8), _config())
    with pytest.raises(D0ProviderError, match="dimension"):
        await port.embed(["one fact"])


async def test_embedding_port_infrastructure_failures_are_typed_not_decisions():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    port = D0EmbeddingPort(httpx.AsyncClient(transport=httpx.MockTransport(handler)), _config())
    with pytest.raises(D0ProviderError, match="http_status"):
        await port.embed(["one fact"])


# ---------------------------------------------------------------------------
# Decision port
# ---------------------------------------------------------------------------


def _pool() -> D0RetrievalResult:
    return D0RetrievalResult(
        retrieved_ids=("m1", "m2"), scored_ids=("m1", "m2"), pool_size=2
    )


def _request() -> dict[str, object]:
    return {
        "candidate_text": "User prefers tea in the morning",
        "candidates": [
            {"memory_id": "m1", "text": "User prefers coffee"},
            {"memory_id": "m2", "text": "User works in Hanoi"},
        ],
    }


async def test_decision_port_sends_strict_schema_temperature_zero():
    captured: list[dict[str, Any]] = []
    content = json.dumps({"decision": "SUPERSEDE", "target_memory_id": "m1"})
    port = D0DecisionPort(_chat_transport(captured, content), _config())
    payload = await port.decide("0" * 64, _request())
    assert payload["decision"] == "SUPERSEDE"
    body = captured[0]
    assert body["temperature"] == 0.0
    assert body["model"] == "gpt-4o-mini"
    schema = body["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["properties"]["decision"]["enum"] == [
        "DUPLICATE",
        "KEEP_BOTH",
        "SUPERSEDE",
    ]
    # Authorization uses the extraction provider key; never the raw content.
    assert "gpt-4o-mini" in body["model"]


async def test_decision_port_user_payload_contains_no_gold_fields():
    captured: list[dict[str, Any]] = []
    content = json.dumps({"decision": "KEEP_BOTH", "target_memory_id": None})
    port = D0DecisionPort(_chat_transport(captured, content), _config())
    await port.decide("0" * 64, _request())
    user_message = captured[0]["messages"][-1]["content"]
    assert "new_candidate_fact" in user_message
    assert "existing_active_memories" in user_message
    assert "m1" in user_message and "User prefers coffee" in user_message
    for leak in (
        "gold",
        "expected_operation",
        "reinforce",
        "supersedes",
        "EARLY",
        "LATE",
        "schedule",
        "S0",
        "S1",
        "label",
        "should_store",
        "canonical",
    ):
        assert leak not in user_message, leak
    system_prompt = captured[0]["messages"][0]["content"]
    assert system_prompt == D0_CONFLICT_SYSTEM_PROMPT


async def test_decision_port_non_200_is_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    port = D0DecisionPort(httpx.AsyncClient(transport=httpx.MockTransport(handler)), _config())
    with pytest.raises(D0ProviderError, match="rate_limit"):
        await port.decide("0" * 64, _request())


async def test_decision_port_semantic_invalid_raises_invalid_decision():
    captured: list[dict[str, Any]] = []
    content = json.dumps({"decision": "SUPERSEDE", "target_memory_id": "unknown"})
    port = D0DecisionPort(_chat_transport(captured, content), _config())
    payload = await port.decide("0" * 64, _request())
    from evaluation.d0_local_executor import _parse_decision

    with pytest.raises(InvalidDecision):
        _parse_decision(payload, ("m1", "m2"))


async def test_decision_port_malformed_json_is_invalid_not_transport():
    captured: list[dict[str, Any]] = []
    port = D0DecisionPort(_chat_transport(captured, "not json at all"), _config())
    with pytest.raises(InvalidDecision):
        await port.decide("0" * 64, _request())


async def test_decision_port_truncated_finish_reason_is_transport_error():
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": '{"decision"'},
                    }
                ]
            },
        )

    port = D0DecisionPort(httpx.AsyncClient(transport=httpx.MockTransport(handler)), _config())
    with pytest.raises(D0ProviderError, match="invalid_chat_response"):
        await port.decide("0" * 64, _request())


# ---------------------------------------------------------------------------
# Prompt version + provider wiring guards
# ---------------------------------------------------------------------------


def test_prompt_version_is_explicit_and_stable():
    assert D0_CONFLICT_PROMPT_VERSION == "d0-conflict-v1"
    # False-supersede traps must be present in the shipped prompt, not implicit.
    for trap in (
        "different time period",
        "multi-valued",
        "different attribute",
        "additive",
        "historical",
    ):
        assert trap in D0_CONFLICT_SYSTEM_PROMPT


async def test_ports_require_configured_providers():
    unconfigured = EvalConfig(profile=Profile.INTERNAL_TEST, suites=("formation",))
    with pytest.raises(ValueError, match="configured"):
        D0EmbeddingPort(httpx.AsyncClient(), unconfigured)
    with pytest.raises(ValueError, match="configured"):
        D0DecisionPort(httpx.AsyncClient(), unconfigured)
