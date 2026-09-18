"""Locked formation headline reporting and bounded candidate registry tests."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from evaluation.formation_report import (
    FormationCandidateRegistry,
    FormationCaseReportInput,
    FormationEvaluationContract,
    build_formation_report,
    declare_formation_candidate,
    render_formation_report_markdown,
    rendered_config_sha256,
    rendered_prompt_sha256,
)
from evaluation.models import BenchmarkVariant, Outcome
from evaluation.scoring import (
    FormationMatchDecision,
    FormationMatchVerdict,
    JudgeProvenance,
    score_formation,
)

_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _judge() -> JudgeProvenance:
    return JudgeProvenance(
        provider="internal-vllm",
        model="judge-model",
        prompt_sha256="a" * 64,
        response_schema_sha256="b" * 64,
    )


def _no_match(index: int = 0) -> FormationMatchDecision:
    return FormationMatchDecision(
        prediction_index=index,
        verdict=FormationMatchVerdict.NO_MATCH,
        reason_code="different_fact",
        judge=_judge(),
    )


def _uncertain(index: int = 0) -> FormationMatchDecision:
    return FormationMatchDecision(
        prediction_index=index,
        verdict=FormationMatchVerdict.UNCERTAIN,
        reason_code="ambiguous_equivalence",
        judge=_judge(),
    )


def test_formation_report_has_only_locked_headlines_with_explicit_denominators():
    exact = score_formation({"conv01:M01": "Ưu tiên Hà Nội"}, ["Ưu tiên Hà Nội"])
    mismatch = score_formation(
        {"conv01:M02": "Ngưỡng 95%"},
        ["Ngưỡng 90%"],
        judge_decisions=(_no_match(),),
    )
    uncertain = score_formation(
        {"conv02:M01": "Trình bày súc tích"},
        ["Trả lời ngắn gọn"],
        judge_decisions=(_uncertain(),),
    )
    report = build_formation_report(
        run_id=_RUN_ID,
        variant=BenchmarkVariant.WORKING_TREE,
        candidate_id="prompt-v1",
        cases=(
            FormationCaseReportInput(
                case_id="conv01:formation:M01",
                bundle_id="conv01",
                family_id="conv01:family:preference",
                outcome=Outcome.PASS,
                score=exact,
            ),
            FormationCaseReportInput(
                case_id="conv01:formation:M02",
                bundle_id="conv01",
                family_id="conv01:family:preference",
                outcome=Outcome.FAIL,
                score=mismatch,
                safety_violation_codes=("secret_memory",),
                audit_required=True,
                audit_completed=True,
            ),
            FormationCaseReportInput(
                case_id="conv02:formation:M01",
                bundle_id="conv02",
                family_id="conv02:family:format",
                outcome=Outcome.REVIEW_REQUIRED,
                score=uncertain,
                audit_required=True,
            ),
            FormationCaseReportInput(
                case_id="conv03:formation:M01",
                bundle_id="conv03",
                family_id="conv03:family:dependency",
                outcome=Outcome.DEPENDENCY_ERROR,
                reason_codes=("formation_provider_error",),
            ),
            FormationCaseReportInput(
                case_id="conv04:formation:M01",
                bundle_id="conv04",
                family_id="conv04:family:protocol",
                outcome=Outcome.PROTOCOL_ERROR,
                reason_codes=("formation_malformed_extraction",),
            ),
        ),
    )

    assert report.eligible_cases == report.attempted_cases == 5
    assert report.scored_cases == 2
    assert report.headline.precision.model_dump() == {
        "name": "formation_precision",
        "value": 0.5,
        "numerator": 1,
        "denominator": 2,
    }
    assert report.headline.recall.value == 0.5
    assert report.headline.f1.value == 0.5
    assert report.headline.f1.numerator == 2
    assert report.headline.f1.denominator == 4
    assert report.headline.safety_failure_cases == 1
    assert report.headline.dependency_error_cases == 1
    assert report.headline.protocol_error_cases == 1
    assert report.headline.uncertain_cases == 1
    assert report.headline.audit_coverage.value == 0.5
    assert report.headline.audit_coverage.denominator == 2
    assert [(group.bundle_id, group.family_id) for group in report.failure_appendix] == [
        ("conv01", "conv01:family:preference"),
        ("conv02", "conv02:family:format"),
        ("conv03", "conv03:family:dependency"),
        ("conv04", "conv04:family:protocol"),
    ]
    assert report.failure_appendix[0].reason_counts == {
        "formation_quality_mismatch": 1,
        "secret_memory": 1,
    }

    markdown = render_formation_report_markdown(report)
    for headline in (
        "Precision",
        "Recall",
        "F1",
        "Safety failure cases",
        "Dependency error cases",
        "Protocol error cases",
        "Uncertain cases",
        "Audit coverage",
    ):
        assert headline in markdown
    assert "overall score" not in markdown.casefold()
    assert "composite" not in markdown.casefold()
    assert "conv01:family:preference" in markdown


def test_formation_report_keeps_empty_denominators_na_and_rejects_invalid_case_state():
    report = build_formation_report(
        run_id=_RUN_ID,
        variant=BenchmarkVariant.WORKING_TREE,
        cases=(
            FormationCaseReportInput(
                case_id="conv01:formation:negative",
                bundle_id="conv01",
                family_id="conv01:family:negative",
                outcome=Outcome.PASS,
                score=score_formation({}, []),
            ),
        ),
    )

    assert report.headline.precision.value is None
    assert report.headline.recall.value is None
    assert report.headline.f1.value is None
    assert report.headline.audit_coverage.value is None
    with pytest.raises(ValidationError, match="scores must appear together"):
        FormationCaseReportInput(
            case_id="conv01:formation:invalid",
            bundle_id="conv01",
            family_id="conv01:family:invalid",
            outcome=Outcome.DEPENDENCY_ERROR,
            score=score_formation({}, []),
        )


def _contract() -> FormationEvaluationContract:
    return FormationEvaluationContract(
        scorer_sha256="c" * 64,
        judge_prompt_sha256="d" * 64,
        judge_schema_sha256="e" * 64,
    )


def test_candidate_registry_hashes_rendered_values_and_allows_at_most_two_overlays():
    control_prompt = "Extract only durable user facts."
    control_config = {"temperature": 0, "max_tokens": 500}
    contract = _contract()
    prompt_candidate = declare_formation_candidate(
        candidate_id="prompt-v1",
        rendered_prompt="Extract durable explicit user facts only.",
        rendered_config=control_config,
        declared_differences=("Tighten explicit-user attribution wording.",),
        runtime_scope=("prompt",),
        evaluation_contract=contract,
    )
    config_candidate = declare_formation_candidate(
        candidate_id="config-v1",
        rendered_prompt=control_prompt,
        rendered_config={"max_tokens": 400, "temperature": 0},
        declared_differences=("Reduce maximum extraction tokens from 500 to 400.",),
        runtime_scope=("config",),
        evaluation_contract=contract,
    )
    registry = FormationCandidateRegistry(
        control_rendered_prompt_sha256=rendered_prompt_sha256(control_prompt),
        control_rendered_config_sha256=rendered_config_sha256(control_config),
        evaluation_contract=contract,
        candidates=(prompt_candidate, config_candidate),
    )

    assert len(registry.candidates) == 2
    assert all(candidate.evaluation_contract == contract for candidate in registry.candidates)
    serialized = registry.model_dump_json()
    assert control_prompt not in serialized
    assert "max_tokens" not in serialized
    assert rendered_config_sha256({"a": 1, "b": 2}) == rendered_config_sha256({"b": 2, "a": 1})
    with pytest.raises(ValidationError, match="at most 2 items"):
        FormationCandidateRegistry(
            control_rendered_prompt_sha256=registry.control_rendered_prompt_sha256,
            control_rendered_config_sha256=registry.control_rendered_config_sha256,
            evaluation_contract=contract,
            candidates=(prompt_candidate, config_candidate, prompt_candidate),
        )


def test_candidate_registry_rejects_undeclared_hash_changes_and_contract_drift():
    contract = _contract()
    control_prompt_hash = rendered_prompt_sha256("control")
    control_config_hash = rendered_config_sha256({"temperature": 0})
    candidate = declare_formation_candidate(
        candidate_id="bad-scope",
        rendered_prompt="changed",
        rendered_config={"temperature": 0},
        declared_differences=("Change prompt.",),
        runtime_scope=("config",),
        evaluation_contract=contract,
    )
    with pytest.raises(ValidationError, match="prompt hash"):
        FormationCandidateRegistry(
            control_rendered_prompt_sha256=control_prompt_hash,
            control_rendered_config_sha256=control_config_hash,
            evaluation_contract=contract,
            candidates=(candidate,),
        )

    drifted = candidate.model_copy(
        update={
            "candidate_id": "contract-drift",
            "runtime_scope": ("prompt",),
            "evaluation_contract": contract.model_copy(update={"scorer_sha256": "f" * 64}),
        }
    )
    with pytest.raises(ValidationError, match="share one scorer"):
        FormationCandidateRegistry(
            control_rendered_prompt_sha256=control_prompt_hash,
            control_rendered_config_sha256=control_config_hash,
            evaluation_contract=contract,
            candidates=(drifted,),
        )


def test_candidate_hash_helpers_reject_blank_prompt_and_non_json_config():
    with pytest.raises(ValueError, match="must not be blank"):
        rendered_prompt_sha256("   ")
    with pytest.raises(ValueError, match="canonical JSON"):
        rendered_config_sha256({"unsupported": object()})
