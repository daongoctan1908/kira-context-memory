"""Deterministic scorecard and internal-judge boundary tests."""

import pytest
from pydantic import ValidationError

from evaluation.models import Profile, Suite
from evaluation.scoring import (
    FormationMatchDecision,
    FormationMatchVerdict,
    JudgeProvenance,
    JudgeVerdict,
    SemanticJudgment,
    mean_metric,
    normalize_exact,
    output_sha256,
    score_constraints,
    score_formation,
    score_retrieval,
    score_retrieval_groups,
    score_safety,
    score_task_success,
)

_HASH = "a" * 64


def _judge() -> JudgeProvenance:
    return JudgeProvenance(
        provider="internal-vllm",
        model="judge-model",
        deployment="judge-test",
        prompt_sha256=_HASH,
        response_schema_sha256="b" * 64,
    )


def test_normalized_exact_preserves_semantic_symbols_and_output_hash_is_stable():
    assert normalize_exact("  Tỷ lệ   >= 98% \n") == normalize_exact("tỷ lệ >= 98%")
    assert normalize_exact("x >= 98%") != normalize_exact("x > 98%")
    assert output_sha256({"b": 2, "a": "đúng"}) == output_sha256({"a": "đúng", "b": 2})


def test_formation_scores_exact_matches_one_to_one_and_duplicate_as_false_positive():
    score = score_formation(
        {"g1": "Ưu tiên Hà Nội", "g2": "Ngưỡng >= 98%"},
        [" ưu tiên   hà nội ", "Ngưỡng >= 98%", "Ưu tiên Hà Nội"],
    )

    assert score.complete
    assert (score.true_positive, score.false_positive, score.false_negative) == (2, 1, 0)
    assert score.precision == pytest.approx(2 / 3)
    assert score.recall == 1
    assert score.f1 == pytest.approx(0.8)
    assert [match.source for match in score.matches] == [
        "exact_normalized",
        "exact_normalized",
    ]


def test_formation_defers_only_semantic_leftovers_to_internal_judge():
    pending = score_formation({"g1": "Trả lời ngắn gọn"}, ["Phản hồi súc tích"])
    assert not pending.complete
    assert pending.needs_judge_prediction_indexes == (0,)
    assert pending.precision is None

    score = score_formation(
        {"g1": "Trả lời ngắn gọn"},
        ["Phản hồi súc tích"],
        judge_decisions=(
            FormationMatchDecision(
                prediction_index=0,
                verdict=FormationMatchVerdict.MATCH,
                gold_id="g1",
                reason_code="semantic_equivalent",
                judge=_judge(),
            ),
        ),
    )
    assert score.complete
    assert score.true_positive == 1
    assert score.f1 == 1
    assert score.matches[0].source == "internal_judge"


def test_formation_uncertain_is_not_silently_scored_and_invalid_mapping_fails():
    uncertain = FormationMatchDecision(
        prediction_index=0,
        verdict=FormationMatchVerdict.UNCERTAIN,
        reason_code="ambiguous_fact_boundary",
        judge=_judge(),
    )
    score = score_formation({"g1": "A"}, ["B"], judge_decisions=(uncertain,))
    assert not score.complete
    assert score.uncertain_prediction_indexes == (0,)

    with pytest.raises(ValueError, match="unknown prediction"):
        score_formation(
            {"g1": "A"},
            ["B"],
            judge_decisions=(uncertain.model_copy(update={"prediction_index": 1}),),
        )
    with pytest.raises(ValidationError):
        FormationMatchDecision(
            prediction_index=0,
            verdict=FormationMatchVerdict.NO_MATCH,
            gold_id="g1",
            reason_code="unsupported",
            judge=_judge(),
        )


def test_negative_formation_case_is_complete_with_na_precision_recall():
    score = score_formation({}, [])
    assert score.complete
    assert (score.true_positive, score.false_positive, score.false_negative) == (0, 0, 0)
    assert score.precision is None
    assert score.recall is None
    assert score.f1 is None


def _decision(index, verdict, gold_id=None):
    return FormationMatchDecision(
        prediction_index=index,
        verdict=verdict,
        gold_id=gold_id,
        reason_code="source_evidence",
        judge=_judge(),
    )


def test_open_world_extra_requires_judge_even_after_all_gold_matches():
    pending = score_formation({"g1": "A"}, ["A", "B"], contract="open_world")
    assert not pending.complete
    assert pending.needs_judge_prediction_indexes == (1,)
    assert pending.scoring_contract == "formation-open-world-v2"
    score = score_formation(
        {"g1": "A"},
        ["A", "B"],
        contract="open_world",
        judge_decisions=(_decision(1, FormationMatchVerdict.VALID_EXTRA),),
    )
    assert score.complete
    assert (score.true_positive, score.valid_extra, score.false_positive) == (1, 1, 0)
    assert score.valid_extra_prediction_indexes == (1,)
    assert score.precision == score.recall == 1


def test_open_world_valid_extra_does_not_replace_missing_gold_or_hide_invalid_extra():
    score = score_formation(
        {"g1": "A"},
        ["B", "unsupported"],
        contract="open_world",
        judge_decisions=(
            _decision(0, FormationMatchVerdict.VALID_EXTRA),
            _decision(1, FormationMatchVerdict.NO_MATCH),
        ),
    )
    assert (score.true_positive, score.valid_extra, score.false_positive, score.false_negative) == (
        0,
        1,
        1,
        1,
    )
    assert score.precision == 0.5
    assert score.recall == 0
    assert score.f1 == 0


def test_open_world_negative_uses_source_validity_instead_of_blanket_empty_gold():
    pending = score_formation({}, ["valid convention"], contract="open_world")
    assert pending.needs_judge_prediction_indexes == (0,)
    accepted = score_formation(
        {},
        ["valid convention"],
        contract="open_world",
        judge_decisions=(_decision(0, FormationMatchVerdict.VALID_EXTRA),),
    )
    assert accepted.complete
    assert accepted.false_positive == accepted.false_negative == 0
    assert accepted.valid_extra == 1
    assert accepted.precision == 1
    assert accepted.recall is None
    uncertain = score_formation(
        {},
        ["ambiguous"],
        contract="open_world",
        judge_decisions=(_decision(0, FormationMatchVerdict.UNCERTAIN),),
    )
    assert not uncertain.complete
    assert uncertain.uncertain_prediction_indexes == (0,)
    with pytest.raises(ValueError, match="open-world"):
        score_formation(
            {},
            ["valid convention"],
            judge_decisions=(_decision(0, FormationMatchVerdict.VALID_EXTRA),),
        )


def test_open_world_same_event_duplicate_cannot_gain_valid_extra_credit():
    pending = score_formation({"g1": "A"}, ["A", " a "], contract="open_world")
    assert pending.complete
    assert pending.true_positive == 1
    assert pending.false_positive == 1
    assert pending.duplicate_prediction_indexes == (1,)
    assert pending.needs_judge_prediction_indexes == ()
    with pytest.raises(ValueError, match="duplicate prediction"):
        score_formation(
            {"g1": "A"},
            ["A", " a "],
            contract="open_world",
            judge_decisions=(_decision(1, FormationMatchVerdict.VALID_EXTRA),),
        )
    # Independent events are scored independently; text equality across events is irrelevant.
    assert score_formation({"e1": "A"}, ["A"], contract="open_world").true_positive == 1
    assert score_formation({"e2": "A"}, ["A"], contract="open_world").true_positive == 1


def test_retrieval_uses_recall_at_3_and_mrr_at_10_only():
    score = score_retrieval(["m1", "m2"], ["noise", "m2", "m2", "m1"])
    assert score.recall_at_3 == pytest.approx(0.5)
    assert score.reciprocal_rank == pytest.approx(0.5)
    assert score.first_relevant_rank == 2
    assert score.mrr_depth == 10

    miss = score_retrieval(["m1"], [f"noise-{index}" for index in range(11)] + ["m1"])
    assert miss.recall_at_3 == 0
    assert miss.reciprocal_rank == 0
    assert miss.first_relevant_rank is None

    no_hit = score_retrieval([], ["unexpected"])
    assert no_hit.recall_at_3 is None
    assert no_hit.reciprocal_rank is None

    grouped = score_retrieval_groups(
        ["m1", "m2"],
        [("noise",), ("m1", "m2")],
    )
    assert grouped.recall_at_3 == 1
    assert grouped.reciprocal_rank == pytest.approx(0.5)


def test_rewrite_constraints_do_not_claim_semantic_equivalence():
    score = score_constraints(
        "So sánh FTTH tại Hà Nội; không dùng dữ liệu 5G.",
        required_exact=("FTTH", "Hà Nội"),
        forbidden=("TP.HCM",),
    )
    assert score.passed
    failed = score_constraints(
        "So sánh FTTH tại TP.HCM", required_exact=("Hà Nội",), forbidden=("TP.HCM",)
    )
    assert not failed.passed
    assert failed.missing_required == ("Hà Nội",)
    assert failed.present_forbidden == ("TP.HCM",)


def test_task_success_requires_action_and_expected_api_subset():
    expected = {"service_id": "413", "condition": [], "nested": {"province": "HNI"}}
    passed = score_task_success(
        expected_action="execute",
        actual_action="execute",
        expected_api=expected,
        actual_api={**expected, "extra": "allowed"},
    )
    assert passed.passed
    assert passed.api_matches

    failed = score_task_success(
        expected_action="execute",
        actual_action="execute",
        expected_api=expected,
        actual_api={"service_id": "414", "condition": [], "nested": {}},
    )
    assert not failed.passed
    assert set(failed.mismatch_paths) == {"api.service_id", "api.nested.province"}


def test_safety_is_a_hard_gate_and_never_an_aggregate_score():
    assert score_safety(()).passed
    failed = score_safety(("cross_user_leak", "cross_user_leak", "secret_memory"))
    assert not failed.passed
    assert failed.violation_codes == ("cross_user_leak", "secret_memory")


def test_semantic_judgment_is_internal_only_and_bound_to_output_hash():
    judgment = SemanticJudgment(
        case_id="case-1",
        suite=Suite.REWRITE,
        output_sha256=_HASH,
        verdict=JudgeVerdict.PASS,
        reason_code="preserves_intent",
        rationale="Intent and required slots are preserved.",
        judge=_judge(),
    )
    assert judgment.judge.profile is Profile.INTERNAL_TEST
    with pytest.raises(ValidationError):
        JudgeProvenance(
            profile=Profile.EXTERNAL_SYNTHETIC,
            provider="openai",
            model="external-model",
            prompt_sha256=_HASH,
            response_schema_sha256=_HASH,
        )
    with pytest.raises(ValidationError):
        SemanticJudgment(
            case_id="case-1",
            suite=Suite.RETRIEVAL,
            output_sha256=_HASH,
            verdict=JudgeVerdict.PASS,
            reason_code="invalid_stage",
            rationale="Retrieval does not use a semantic judge.",
            judge=_judge(),
        )


def test_metric_aggregation_excludes_na_without_turning_it_into_zero():
    assert mean_metric((1.0, None, 0.5)) == pytest.approx(0.75)
    assert mean_metric((None, None)) is None
    with pytest.raises(ValueError, match="finite"):
        mean_metric((float("inf"),))
