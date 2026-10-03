"""The comparison must hold inputs fixed and never promote a semantic loss."""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

import scripts.benchmark.compare_rewrite_context as comparison
from evaluation.config import EvalConfig, ProviderConfig
from evaluation.models import Profile
from evaluation.scoring import JudgeVerdict
from scripts.benchmark.compare_rewrite_context import (
    ARMS,
    DEFAULT_FIXTURE,
    build_arm,
    compare,
    load_fixture,
    summarize,
)


def test_frozen_control_and_reassertion_are_faithful():
    fixture = load_fixture(DEFAULT_FIXTURE)
    case = next(case for case in fixture["cases"] if case["id"] == "reassert_98")
    old_context, old_messages = build_arm(fixture, case, "CONTROL")
    new_context, new_messages = build_arm(fixture, case, "NEW_10")
    assert [memory.memory_id for memory in old_context.long_term_memories] == ["b", "a"]
    assert [memory.memory_id for memory in new_context.long_term_memories] == ["c", "b", "a"]
    assert old_messages[0]["content"] == fixture["control_system_prompt"]
    old_data = json.loads(old_messages[1]["content"])
    new_data = json.loads(new_messages[1]["content"])
    assert all(isinstance(memory, str) for memory in old_data["long_term_memories"])
    assert new_data["long_term_memories"][0]["source_timestamp"] is not None
    assert new_data["current_query"] == old_data["current_query"] == case["query"]


def test_windows_keep_complete_turns_and_do_not_invent_retrieved_memories():
    fixture = load_fixture(DEFAULT_FIXTURE)
    before = copy.deepcopy(fixture)
    case = next(case for case in fixture["cases"] if case["id"] == "antecedent_5_turns")
    for arm, limit in ARMS.items():
        context, _ = build_arm(fixture, case, arm)
        assert len(context.recent_messages) == limit
        assert context.recent_messages[0].role.value == "user"
        assert not context.long_term_memories
    assert fixture == before


@pytest.mark.parametrize("change", ["control", "naive_timestamp", "turn_pair"])
def test_invalid_or_modified_fixtures_are_rejected(tmp_path, change):
    fixture = load_fixture(DEFAULT_FIXTURE)
    case = next(case for case in fixture["cases"] if case["recent_messages"])
    if change == "control":
        fixture["control_system_prompt"] += "changed"
    elif change == "naive_timestamp":
        case["recent_messages"][0]["timestamp"] = "2026-09-14T00:00:00"
    else:
        case["recent_messages"][1]["turn_id"] = "different-turn"
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(ValueError):
        load_fixture(path)


def _results(fixture):
    return [
        {
            "case_id": case["id"],
            "arm": arm,
            "repeat": 0,
            "outcome": "pass",
            "latency_ms": 10.0,
            "input_tokens": limit * 20,
            "prompt_bytes": limit * 100,
        }
        for case in fixture["cases"]
        for arm, limit in ARMS.items()
    ]


def test_equal_quality_with_fewer_tokens_is_eligible_without_semantic_gain():
    fixture = load_fixture(DEFAULT_FIXTURE)
    summary = summarize(fixture, _results(fixture), 1)
    assert summary["quality_and_size_eligible"] == ["NEW_4", "NEW_6", "NEW_10"]
    assert summary["candidate_max_recent_messages"] == 4
    assert summary["deployment_max_recent_messages"] == 10
    assert summary["promotion"] == "manual_latency_review_required"


@pytest.mark.parametrize("tie", [False, True])
def test_window_selection_uses_measured_total_tokens_and_prefers_larger_ties(tie):
    fixture = load_fixture(DEFAULT_FIXTURE)
    rows = _results(fixture)
    for row in rows:
        if row["arm"] == "NEW_4":
            row["input_tokens"] = 100 if tie else 180
        elif row["arm"] == "NEW_6":
            row["input_tokens"] = 100

    summary = summarize(fixture, rows, 1)

    assert summary["quality_and_size_eligible"] == ["NEW_4", "NEW_6", "NEW_10"]
    assert summary["candidate_max_recent_messages"] == 6
    assert summary["arms"]["NEW_6"]["input_tokens_median"] == 100
    assert summary["arms"]["NEW_6"]["input_tokens_total"] == 100 * len(fixture["cases"])


@pytest.mark.parametrize(
    "malformation",
    ["duplicate", "missing_arm", "unknown_case", "unknown_arm", "boolean_repeat", "extra_repeat"],
)
def test_duplicate_or_malformed_result_identity_blocks_a_complete_run(malformation):
    fixture = load_fixture(DEFAULT_FIXTURE)
    rows = _results(fixture)
    if malformation == "duplicate":
        rows.append(dict(rows[0]))
    elif malformation == "missing_arm":
        del rows[0]["arm"]
    elif malformation == "unknown_case":
        rows[0]["case_id"] = "unexpected-case"
    elif malformation == "unknown_arm":
        rows[0]["arm"] = "unexpected-arm"
    elif malformation == "boolean_repeat":
        rows[0]["repeat"] = False
    else:
        rows[0]["repeat"] = 1

    summary = summarize(fixture, rows, 1)

    assert summary["complete"] is False
    assert summary["invalid_result_rows"] == 1
    assert summary["quality_and_size_eligible"] == []
    assert summary["candidate_max_recent_messages"] == 10


def test_a_single_quality_loss_cannot_be_offset_by_aggregate_gain():
    fixture = load_fixture(DEFAULT_FIXTURE)
    rows = _results(fixture)
    row = next(row for row in rows if row["arm"] == "NEW_4")
    row["outcome"] = "fail"
    summary = summarize(fixture, rows, 1)
    assert "NEW_4" not in summary["quality_and_size_eligible"]
    assert summary["candidate_max_recent_messages"] == 6
    assert not summarize(fixture, rows[:-1], 1)["complete"]


def test_prompt_bytes_without_token_measurements_cannot_promote_shorter_window():
    fixture = load_fixture(DEFAULT_FIXTURE)
    rows = _results(fixture)
    for row in rows:
        row["input_tokens"] = None
    summary = summarize(fixture, rows, 1)
    assert summary["quality_and_size_eligible"] == ["NEW_10"]
    assert summary["candidate_max_recent_messages"] == 10
    assert summary["arms"]["NEW_4"]["input_tokens_total"] is None
    assert summary["arms"]["NEW_4"]["input_tokens_median"] is None
    assert summary["arms"]["NEW_4"]["input_tokens_samples"] == 0


def test_missing_usage_does_not_report_a_partial_token_sum_as_the_total():
    fixture = load_fixture(DEFAULT_FIXTURE)
    rows = _results(fixture)
    next(row for row in rows if row["arm"] == "NEW_4")["input_tokens"] = None

    summary = summarize(fixture, rows, 1)

    assert summary["arms"]["NEW_4"]["input_tokens_available"] is False
    assert summary["arms"]["NEW_4"]["input_tokens_samples"] == len(fixture["cases"]) - 1
    assert summary["arms"]["NEW_4"]["input_tokens_median"] == 80
    assert summary["arms"]["NEW_4"]["input_tokens_total"] is None
    assert summary["candidate_max_recent_messages"] == 6


async def test_native_adapter_request_settings_and_blind_judge_are_shared(monkeypatch):
    monkeypatch.setattr(comparison, "REWRITE_PROMPT_VERSION", "runtime-version")
    fixture = load_fixture(DEFAULT_FIXTURE)
    fixture["cases"] = [fixture["cases"][0]]
    requests = []

    def response(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Doanh thu Đà Nẵng tháng 7/2026?"},
                    }
                ],
                "usage": {"prompt_tokens": 100},
            },
        )

    judgments = []

    class Judge:
        async def semantic(self, **kwargs):
            judgments.append(kwargs)
            return SimpleNamespace(verdict=JudgeVerdict.PASS, reason_code="matches")

    config = EvalConfig(
        profile=Profile.INTERNAL_TEST,
        rewrite=ProviderConfig(base_url="http://rewrite.invalid/v1", model="same-model"),
    )
    report = await compare(
        fixture,
        config,
        repetitions=2,
        transport=httpx.MockTransport(response),
        judge=Judge(),
    )
    assert len(requests) == len(judgments) == 8
    assert all(request["model"] == "same-model" for request in requests)
    assert all(request["temperature"] == 0 and request["max_tokens"] == 256 for request in requests)
    assert requests[0]["messages"][0]["content"] == fixture["control_system_prompt"]
    assert requests[1]["messages"][0]["content"] != fixture["control_system_prompt"]
    assert all("arm" not in judgment and "repeat" not in judgment for judgment in judgments)
    assert all(row["input_tokens"] == 100 for row in report["results"])
    assert report["summary"]["complete"]
    assert report["candidate_prompt_version"] == "runtime-version"
    assert report["control_prompt_version"] == fixture["control_prompt_version"]
    assert (
        report["candidate_system_prompt_sha256"]
        == hashlib.sha256(comparison.REWRITE_SYSTEM_PROMPT.encode()).hexdigest()
    )
    assert report["control_system_prompt_sha256"] == fixture["control_system_sha256"]


@pytest.mark.parametrize("oversized_latest", [False, True])
async def test_report_records_actual_retained_turns_and_budget_trimming(oversized_latest):
    fixture = load_fixture(DEFAULT_FIXTURE)
    case = next(case for case in fixture["cases"] if case["id"] == "antecedent_3_turns")
    fixture["cases"] = [case]
    if oversized_latest:
        case["recent_messages"][-1]["content"] = "x" * 13_000

    class Judge:
        async def semantic(self, **kwargs):
            return SimpleNamespace(verdict=JudgeVerdict.PASS, reason_code="matches")

    config = EvalConfig(
        profile=Profile.INTERNAL_TEST,
        rewrite=ProviderConfig(base_url="http://rewrite.invalid/v1", model="same-model"),
    )
    report = await compare(
        fixture,
        config,
        repetitions=1,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "Số thuê bao Hưng Yên tháng 8/2026?"},
                        }
                    ],
                    "usage": {"prompt_tokens": 100},
                },
            )
        ),
        judge=Judge(),
    )

    expected = {
        "CONTROL": (10, 5, "none"),
        "NEW_10": (10, 5, "none"),
        "NEW_6": (6, 3, "message_limit"),
        "NEW_4": (4, 2, "message_limit"),
    }
    for row in report["results"]:
        retained = (
            row["recent_retained_messages"],
            row["recent_retained_turns"],
            row["trim_reason"],
        )
        if oversized_latest:
            assert retained == (0, 0, "token_budget")
            assert row["estimated_recent_tokens"] == 0
        else:
            assert retained == expected[row["arm"]]
            assert row["estimated_recent_tokens"] > 0


async def test_errors_and_uncertain_judgments_block_promotion():
    fixture = load_fixture(DEFAULT_FIXTURE)
    fixture["cases"] = [fixture["cases"][0]]
    config = EvalConfig(rewrite=ProviderConfig(base_url="http://rewrite.invalid", model="same"))
    report = await compare(
        fixture,
        config,
        repetitions=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        judge=object(),
    )
    assert not report["summary"]["contract_quality_passed"]
    assert report["summary"]["quality_and_size_eligible"] == []
    assert all(row["outcome"] == "dependency_error" for row in report["results"])
