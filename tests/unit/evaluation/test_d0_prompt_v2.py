"""d0-conflict-v2 prompt regression tests: the 3-way decision boundary rules the
experiment is built on, pinned against SYNTHETIC examples only (no dataset event
IDs, no dataset phrases). Each test names the rule it protects.

The prompt is text, not executable code, so the tests assert on prompt content:
every rule required by the experiment contract must be explicitly present, and the
version registry must keep v1 intact alongside v2 (comparative experiment needs
both). Runtime behavior tests (v2 goes into the system message, feeds the request
hash, and is selectable end to end) run over mocked transports."""

import json

import httpx
import pytest

from evaluation.config import EvalConfig, Profile, ProviderConfig
from evaluation.d0_ports import (
    D0_CONFLICT_SYSTEM_PROMPT,
    D0_CONFLICT_SYSTEM_PROMPT_V2,
    D0DecisionPort,
    resolve_conflict_prompt,
)

V2_RULES = {
    # DUPLICATE boundary
    "same current fact + 'vẫn' confirmation => DUPLICATE": "confirmation",
    "clarification that does not change truth => DUPLICATE": "clarifies",
    "temporary exception ended, durable fact unchanged => DUPLICATE": "temporary",
    # SUPERSEDE boundary
    "same slot + old exclusivity invalidated => SUPERSEDE": "exclusivity",
    "counterfactual inconsistency test for SUPERSEDE": "counterfactual",
    # KEEP_BOTH boundary
    "same acronym/entity, different predicate => KEEP_BOTH": "usage rule",
    "definition and usage convention coexist => KEEP_BOTH": "coexist",
    "same entity, different attribute => KEEP_BOTH": "different attribute",
    "additive info does not automatically supersede": "additive",
    "lexical similarity alone cannot supersede": "Lexical similarity alone",
    # Contract
    "SUPERSEDE requires one target": "exactly one",
    "KEEP_BOTH target=null": "null target",
}

V2_RULE_TEXTS = {
    "same current fact + 'vẫn' confirmation => DUPLICATE": "unchanged",
    "same fact + clarification that does not change truth => DUPLICATE": "is DUPLICATE, not\nSUPERSEDE",
    "temporary exception ended, durable fact unchanged => DUPLICATE": "the\ntemporary exception, still",
    "same slot + old exclusivity invalidated => SUPERSEDE": "SUPERSEDE",
    "counterfactual inconsistency test for SUPERSEDE": "BOTH kept as current facts",
    "same acronym/entity, different predicate => KEEP_BOTH": "usage rule",
    "definition and usage convention coexist => KEEP_BOTH": "coexist",
    "same entity, different attribute => KEEP_BOTH": "different attribute",
    "additive info does not automatically supersede": "additive",
    "lexical similarity alone cannot supersede": "Lexical similarity alone never justifies SUPERSEDE",
    "SUPERSEDE requires one target": "SUPERSEDE and\nDUPLICATE each name exactly one target",
    "KEEP_BOTH target=null": "KEEP_BOTH always has a null target",
}


def test_v2_prompt_contains_all_semantic_rules():
    for rule, needle in V2_RULE_TEXTS.items():
        assert needle in D0_CONFLICT_SYSTEM_PROMPT_V2, f"missing rule: {rule}"


def test_v2_does_not_hardcode_dataset_specifics():
    forbidden = (
        "conv01", "conv02", "conv03", "conv04",
        "DTDV", "Thượng Long", "Thanh Sơn", "Chí Tiên", "Thượng",
        "bộ lõi", "bên mình",
        "M01", "M02", "M03", "M05", "M08", "M09", "M10", "M13", "M14", "M15", "M17",
    )
    for literal in forbidden:
        assert literal not in D0_CONFLICT_SYSTEM_PROMPT_V2, literal


def test_v1_prompt_unchanged_and_v2_registered_separately():
    # v1 stays byte-identical for the comparative experiment (hash pinned in
    # real-run-004's manifest).
    assert resolve_conflict_prompt("d0-conflict-v1") == ("d0-conflict-v1", D0_CONFLICT_SYSTEM_PROMPT)
    assert resolve_conflict_prompt("d0-conflict-v2") == ("d0-conflict-v2", D0_CONFLICT_SYSTEM_PROMPT_V2)
    assert D0_CONFLICT_SYSTEM_PROMPT_V2 != D0_CONFLICT_SYSTEM_PROMPT


def test_unknown_prompt_version_fails_closed():
    with pytest.raises(ValueError, match="unknown D0 conflict prompt version"):
        resolve_conflict_prompt("d0-conflict-v999")


def test_confirmation_cue_family_present():
    cues = ("still", "remains", "continues to be", "has not changed")
    for cue in cues:
        assert cue in D0_CONFLICT_SYSTEM_PROMPT_V2, cue
    assert "Vietnamese equivalents" in D0_CONFLICT_SYSTEM_PROMPT_V2


def _config() -> EvalConfig:
    return EvalConfig(
        profile=Profile.INTERNAL_TEST,
        suites=("formation",),
        extraction=ProviderConfig(
            base_url="https://llm.invalid/v1", model="gpt-4o-mini", api_key="k" * 10
        ),
        embedding=ProviderConfig(
            base_url="https://embed.invalid/v1", model="text-embedding-3-small", api_key="k" * 10
        ),
        embedding_dimensions=1536,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=2.0,
        total_timeout_seconds=5.0,
    )


def _chat_transport(captured: list[dict], content: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ]
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _request() -> dict:
    return {
        "candidate_text": "candidate fact",
        "candidates": [{"memory_id": "m1", "text": "existing fact"}],
    }


async def test_v2_port_sends_v2_system_prompt_and_schema():
    captured: list[dict] = []
    port = D0DecisionPort(
        _chat_transport(captured, '{"decision": "KEEP_BOTH", "target_memory_id": null}'),
        _config(),
        prompt_version="d0-conflict-v2",
    )
    payload = await port.decide("0" * 64, _request())
    assert payload["decision"] == "KEEP_BOTH"
    assert captured[0]["messages"][0]["content"] == D0_CONFLICT_SYSTEM_PROMPT_V2
    schema = captured[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["decision"]["enum"] == ["DUPLICATE", "KEEP_BOTH", "SUPERSEDE"]
    # Response schema must be byte-identical to v1's (§10: no schema change).
    from evaluation.d0_local_executor import CONFLICT_RESPONSE_SCHEMA

    assert captured[0]["response_format"]["json_schema"]["schema"] == CONFLICT_RESPONSE_SCHEMA


async def test_v1_default_port_still_sends_v1_prompt():
    captured: list[dict] = []
    port = D0DecisionPort(
        _chat_transport(captured, '{"decision": "KEEP_BOTH", "target_memory_id": null}'),
        _config(),
    )
    await port.decide("0" * 64, _request())
    assert captured[0]["messages"][0]["content"] == D0_CONFLICT_SYSTEM_PROMPT


def test_v2_version_changes_request_hash():
    from evaluation.d0_local_executor import decision_request
    from evaluation.models import D0RetrievalResult

    pool = D0RetrievalResult(retrieved_ids=("m1",), scored_ids=("m1",), pool_size=1)
    texts = {"m1": "existing fact"}
    h1, _ = decision_request("d0-local", "d0-conflict-v1", "candidate fact", pool, texts)
    h2, _ = decision_request("d0-local", "d0-conflict-v2", "candidate fact", pool, texts)
    assert h1 != h2
