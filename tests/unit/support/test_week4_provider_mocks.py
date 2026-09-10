"""Contracts for the independent deterministic providers in the Week 4 stack."""

import asyncio
import json
from hashlib import sha256

import httpx
import pytest

from tests.support import (
    mock_embedding_server,
    mock_kira_server,
    mock_memory_llm_server,
    mock_vllm_server,
)
from tests.support.week4_cases import (
    memory_fact,
    rewritten_follow_up,
    session_b_follow_up,
)


def memory_request(prompt: str) -> dict[str, object]:
    return {
        "model": "local-memory-stub",
        "messages": [
            {"role": "system", "content": "synthetic test"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 1000,
        "response_format": {"type": "json_object"},
    }


@pytest.mark.parametrize(
    "app",
    [
        mock_kira_server.app,
        mock_vllm_server.app,
        mock_embedding_server.app,
        mock_memory_llm_server.app,
    ],
)
async def test_compose_provider_liveness_contract(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://provider.test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_embedding_mock_supports_admin_probe_and_runtime_batch() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_embedding_server.app),
        base_url="http://embedding.test",
    ) as client:
        probe = await client.post(
            "/v1/embeddings",
            json={
                "model": "local-embedding-stub",
                "input": "dimension probe",
                "encoding_format": "float",
            },
        )
        batch = await client.post(
            "/v1/embeddings",
            json={
                "model": "local-embedding-stub",
                "input": ["one", "two"],
                "encoding_format": "float",
                "dimensions": 3,
            },
        )
        invalid = await client.post(
            "/v1/embeddings",
            json={"model": "local-embedding-stub", "input": ["one"], "dimensions": 4},
        )

    assert probe.status_code == 200
    assert len(probe.json()["data"][0]["embedding"]) == 3
    assert [item["index"] for item in batch.json()["data"]] == [0, 1]
    assert invalid.status_code == 400


async def test_memory_llm_mock_extracts_only_explicit_synthetic_marker() -> None:
    marked_fact = "The local user's preferred synthetic region is North."
    prompt = (
        "## Summary\n\n\n"
        "## New Messages\n"
        + json.dumps(
            [
                {"role": "user", "content": f"T4_E2E_MEMORY: {marked_fact}"},
                {"role": "assistant", "content": "Acknowledged."},
            ]
        )
        + "\n\n## Observation Date\n2026-09-10"
    ).replace("\n", "\r\n")
    request = memory_request(prompt)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_memory_llm_server.app),
        base_url="http://memory-llm.test",
    ) as client:
        response = await client.post("/v1/chat/completions", json=request)
        request["messages"][1]["content"] = prompt.replace("T4_E2E_MEMORY:", "ordinary query:")
        ignored = await client.post("/v1/chat/completions", json=request)

    assert response.status_code == 200
    extracted = json.loads(response.json()["choices"][0]["message"]["content"])
    assert extracted == {"memory": [{"text": marked_fact, "attributed_to": "user"}]}
    ignored_content = json.loads(ignored.json()["choices"][0]["message"]["content"])
    assert ignored_content == {"memory": []}


async def test_memory_llm_mock_accepts_native_mem0_message_format_and_block_control() -> None:
    fact = "Synthetic native Mem0 fact."
    prompt = (
        "## New Messages\n"
        f"user: T4_E2E_MEMORY: {fact}\n"
        "assistant: acknowledged\n\n"
        "## Observation Date\n2026-09-10"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_memory_llm_server.app),
        base_url="http://memory-llm.test",
    ) as client:
        reset = await client.post("/_test/reset", json={"block": True})
        pending = asyncio.create_task(
            client.post("/v1/chat/completions", json=memory_request(prompt))
        )
        await asyncio.sleep(0)
        state = (await client.get("/_test/state")).json()

        assert reset.status_code == 200
        assert pending.done() is False
        assert state == {
            "stub": "memory-llm-local-only",
            "request_count": 1,
            "blocked_request_count": 1,
            "released": False,
        }

        assert (await client.post("/_test/release")).status_code == 200
        response = await asyncio.wait_for(pending, timeout=1)

    extracted = json.loads(response.json()["choices"][0]["message"]["content"])
    assert extracted == {"memory": [{"text": fact, "attributed_to": "user"}]}


async def test_rewriter_mock_uses_matching_long_term_memory_and_records_only_hashes() -> None:
    run_id = "unit-run"
    current = session_b_follow_up(run_id)
    expected = rewritten_follow_up(run_id)
    payload = {
        "model": "local-contract-stub",
        "messages": [
            {"role": "system", "content": "rewrite only"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_query": current,
                        "recent_messages": [],
                        "long_term_memories": [memory_fact(run_id)],
                    }
                ),
            },
        ],
        "temperature": 0,
        "stream": False,
        "max_tokens": 256,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_vllm_server.app),
        base_url="http://rewriter.test",
    ) as client:
        await client.post("/_test/reset")
        response = await client.post("/v1/chat/completions", json=payload)
        evidence = (await client.get("/_test/requests")).json()

    assert response.json()["choices"][0]["message"]["content"] == expected
    assert evidence == {
        "stub": "rewriter-local-only",
        "requests": [
            {
                "current_hash": sha256(current.encode()).hexdigest(),
                "long_term_memory_count": 1,
                "output_hash": sha256(expected.encode()).hexdigest(),
                "recent_message_count": 0,
            }
        ],
    }
