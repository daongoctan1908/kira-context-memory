"""Tests for the holdout evaluation wiring: payload purity, request identity,
adjudication, and fail-closed runner behavior. All LLM interactions run over a
mock transport; no network, no dataset bytes modified."""

import json

import httpx
import pytest

from evaluation.config import EvalConfig, Profile, ProviderConfig
from evaluation.d0_holdout import (
    adjudicate_holdout,
    decide_holdout_case,
    holdout_request_hash,
    load_holdout,
)
from evaluation.d0_ports import D0DecisionPort

EXTRACTION = ProviderConfig(
    base_url="https://llm.invalid/v1", model="gpt-4o-mini", api_key="k" * 10
)


def _config() -> EvalConfig:
    return EvalConfig(
        profile=Profile.INTERNAL_TEST,
        suites=("formation",),
        extraction=EXTRACTION,
        embedding=ProviderConfig(
            base_url="https://embed.invalid/v1", model="text-embedding-3-small", api_key="k" * 10
        ),
        embedding_dimensions=1536,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=2.0,
        total_timeout_seconds=5.0,
    )


def _transport(captured: list[dict], content: str) -> httpx.AsyncClient:
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


async def test_request_contains_only_model_visible_fields():
    _manifest, cases = load_holdout()
    case = cases[0]
    _hash, request = holdout_request_hash("gpt-4o-mini", "d0-conflict-v1", case)
    dumped = json.dumps(request, ensure_ascii=False)
    for forbidden in (
        "gold_decision",
        "gold_target_id",
        "rationale",
        "adversarial_property",
        "theme",
    ):
        assert f'"{forbidden}"' not in dumped, forbidden
    # The request carries exactly the model-visible payload plus request metadata.
    assert sorted(request) == [
        "candidate_text",
        "candidates",
        "decoding_params",
        "model",
        "operations",
        "prompt_version",
        "response_schema_sha256",
    ]
    assert request["candidate_text"] == case.candidate
    assert [entry["memory_id"] for entry in request["candidates"]] == [
        row.memory_id for row in case.existing_active_memories
    ]
    assert [entry["text"] for entry in request["candidates"]] == [
        row.text for row in case.existing_active_memories
    ]


async def test_prompt_version_changes_request_hash_and_cache():
    _manifest, cases = load_holdout()
    case = cases[0]
    h1, _ = holdout_request_hash("gpt-4o-mini", "d0-conflict-v1", case)
    h2, _ = holdout_request_hash("gpt-4o-mini", "d0-conflict-v2", case)
    assert h1 != h2


async def test_decide_holdout_case_parses_and_caches():
    _manifest, cases = load_holdout()
    case = cases[0]
    captured: list[dict] = []
    first_id = case.existing_active_memories[0].memory_id
    content = json.dumps({"decision": "DUPLICATE", "target_memory_id": first_id})
    port = D0DecisionPort(
        _transport(captured, content),
        _config(),
        prompt_version="d0-conflict-v2",
    )
    cache: dict = {}
    p1 = await decide_holdout_case(port, "gpt-4o-mini", "d0-conflict-v2", case, cache)
    assert p1.decision is not None and p1.decision.decision == "DUPLICATE"
    assert p1.cache_hit is False and p1.llm_request_hash is not None
    assert len(captured) == 1
    # Same request identity => cache hit, no second transport call.
    p2 = await decide_holdout_case(port, "gpt-4o-mini", "d0-conflict-v2", case, cache)
    assert p2.cache_hit is True and p2.decision == p1.decision
    assert len(captured) == 1


async def test_decide_holdout_case_invalid_output_is_fail_closed():
    _manifest, cases = load_holdout()
    case = cases[0]
    port = D0DecisionPort(
        _transport([], '{"decision": "MAYBE", "target_memory_id": null}'),
        _config(),
        prompt_version="d0-conflict-v2",
    )
    prediction = await decide_holdout_case(port, "gpt-4o-mini", "d0-conflict-v2", case, {})
    assert prediction.decision is None
    assert prediction.invalid_reason is not None


async def test_decide_holdout_case_rejects_target_outside_pool():
    _manifest, cases = load_holdout()
    case = next(c for c in cases if c.gold_decision != "KEEP_BOTH")
    port = D0DecisionPort(
        _transport([], '{"decision": "SUPERSEDE", "target_memory_id": "t9999"}'),
        _config(),
        prompt_version="d0-conflict-v2",
    )
    prediction = await decide_holdout_case(port, "gpt-4o-mini", "d0-conflict-v2", case, {})
    assert prediction.decision is None
    assert prediction.invalid_reason == "invalid_target"


def test_adjudicate_holdout_exact_and_mismatch_flags():
    _manifest, cases = load_holdout()
    dup = next(c for c in cases if c.gold_decision == "DUPLICATE")
    keep = next(c for c in cases if c.gold_decision == "KEEP_BOTH")
    sup = next(c for c in cases if c.gold_decision == "SUPERSEDE")
    from evaluation.models import D0ConflictDecision

    def _pred(case, decision, target, invalid_reason=None):
        from evaluation.d0_holdout import HoldoutPrediction

        return HoldoutPrediction(
            prompt_version="d0-conflict-v2",
            case_id=case.case_id,
            pool_ids=tuple(row.memory_id for row in case.existing_active_memories),
            decision=None
            if decision is None
            else D0ConflictDecision(decision=decision, target_memory_id=target),
            invalid_reason=invalid_reason,
        )

    exact = adjudicate_holdout(_pred(dup, "DUPLICATE", dup.gold_target_id), dup)
    assert exact["exact"] is True and not exact["false_supersede"]
    second_id = dup.existing_active_memories[1].memory_id
    wrong_target = adjudicate_holdout(_pred(dup, "DUPLICATE", second_id), dup)
    # exact is kind-level (decision label matches gold); target identity is the
    # orthogonal target_mismatch flag, mirroring the D0 harness contract.
    assert wrong_target["exact"] is True and wrong_target["target_mismatch"] is True
    first_keep = keep.existing_active_memories[0].memory_id
    false_sup = adjudicate_holdout(_pred(keep, "SUPERSEDE", first_keep), keep)
    assert false_sup["exact"] is False and false_sup["false_supersede"] is True
    invalid = adjudicate_holdout(_pred(sup, None, None, invalid_reason="invalid_decision"), sup)
    assert invalid["exact"] is None and invalid["invalid"] is True
    kind_wrong = adjudicate_holdout(_pred(sup, "KEEP_BOTH", None), sup)
    assert kind_wrong["decision_mismatch"] is True and not kind_wrong["target_mismatch"]


def test_holdout_runner_rejects_unapproved_slice(tmp_path, monkeypatch):
    import asyncio
    import shutil

    import scripts.benchmark.d0_holdout_run as runner
    from evaluation import d0_holdout as holdout_module
    from evaluation.d0_holdout import HoldoutManifest

    shutil.copytree(holdout_module.HOLDOUT_ROOT, tmp_path / "slice")
    doc = json.loads((tmp_path / "slice" / "manifest.json").read_text(encoding="utf-8"))
    doc["status"] = "draft"
    (tmp_path / "slice" / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    def loader():
        document = json.loads((tmp_path / "slice" / "manifest.json").read_text(encoding="utf-8"))
        return HoldoutManifest.model_validate(document), ()

    # The runner imported load_holdout into its own namespace, so patch there.
    monkeypatch.setattr(runner, "load_holdout", loader)
    with pytest.raises(ValueError, match="not approved_frozen"):
        asyncio.run(
            runner.run_holdout(
                env_file=None,
                exposure="internal",
                prompt_version="d0-conflict-v2",
                decisions_port=object(),
            )
        )
