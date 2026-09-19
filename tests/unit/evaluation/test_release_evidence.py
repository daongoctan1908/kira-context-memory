"""Official confirmation, performance-review and promotion evidence tests."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

import evaluation.release_evidence as release
from evaluation.artifacts import CaseAttemptArtifact
from evaluation.audit import (
    AuditCandidate,
    AuditReconciliation,
    ReconciledAuditCase,
    audit_candidate_set_sha256,
)
from evaluation.cross_session import (
    CrossSessionArmEvaluation,
    CrossSessionCaseEvaluation,
    CrossSessionCondition,
)
from evaluation.formation import (
    ExtractedFact,
    FormationExecutionStatus,
    FormationExtractionResult,
)
from evaluation.models import (
    BenchmarkVariant,
    CaseEligibility,
    CrossSessionInput,
    EvalCase,
    FormationInput,
    GoldFact,
    GoldSpecification,
    Message,
    Outcome,
    PerformanceReviewVerdict,
    Profile,
    RetrievalInput,
    RewriteInput,
    Suite,
)
from evaluation.native_executor import (
    NativeFormationCaseEvaluation,
    NativeRetrievalCaseEvaluation,
)
from evaluation.release_evidence import (
    CleanupEvidence,
    ConfirmationComponent,
    ConfirmationVerdict,
    ExactImageSet,
    PerformanceEvidence,
    PerformanceSample,
    QualityMetric,
    QualityMetricName,
    ReleaseDecision,
    RunQualityEvidence,
    build_confirmation_report,
    build_performance_review,
    build_promotion_report,
    build_run_quality_evidence,
)
from evaluation.retrieval import RetrievalCaseEvaluation, RetrievalMode
from evaluation.rewrite import RewriteCaseEvaluation
from evaluation.scoring import (
    ConstraintScore,
    JudgeProvenance,
    JudgeVerdict,
    RetrievalScore,
    SemanticJudgment,
    TaskSuccessScore,
    output_sha256,
    score_formation,
)
from evaluation.timing import TimingOutcome, TimingStage

_NOW = datetime(2026, 9, 19, tzinfo=UTC)
_SHA = "a" * 64


def _metric(value: float | None, denominator: int = 10) -> QualityMetric:
    return QualityMetric(value=value, denominator=denominator if value is not None else 0)


def _run(
    *,
    variant: BenchmarkVariant,
    number: int,
    primary: float,
    no_ltm: float = 0.4,
    task: float | None = 0.8,
    no_ltm_task: float | None = 0.7,
    safety: tuple[str, ...] = (),
    complete: bool = True,
) -> RunQualityEvidence:
    metrics = {metric: _metric(0.8) for metric in QualityMetricName}
    metrics[QualityMetricName.FORMATION_F1] = _metric(primary)
    metrics[QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE] = _metric(primary)
    metrics[QualityMetricName.NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE] = _metric(no_ltm)
    metrics[QualityMetricName.FINAL_QA_TASK_SUCCESS_RATE] = _metric(task)
    metrics[QualityMetricName.NO_LTM_TASK_SUCCESS_RATE] = _metric(no_ltm_task)
    return RunQualityEvidence(
        run_id=UUID(int=number + (100 if variant is BenchmarkVariant.RELEASE_CANDIDATE else 0)),
        variant=variant,
        candidate_id="candidate-a" if variant is BenchmarkVariant.RELEASE_CANDIDATE else None,
        dataset_sha256="1" * 64,
        compilation_sha256="2" * 64,
        selected_case_ids_sha256="3" * 64,
        seed=number,
        runtime_sha=("4" if variant is BenchmarkVariant.HISTORICAL_CONTROL else "5") * 40,
        harness_sha="6" * 40,
        config_sha256=("7" if variant is BenchmarkVariant.HISTORICAL_CONTROL else "8") * 64,
        metrics=metrics,
        families=(),
        safety_violation_codes=safety,
        unresolved_reason_codes=() if complete else ("missing_evidence",),
        evidence_complete=complete,
    )


def _paired_runs(candidate_values=(0.7, 0.5, 0.8)):
    controls = tuple(
        _run(
            variant=BenchmarkVariant.HISTORICAL_CONTROL,
            number=index,
            primary=0.6,
        )
        for index in range(1, 4)
    )
    candidates = tuple(
        _run(
            variant=BenchmarkVariant.RELEASE_CANDIDATE,
            number=index,
            primary=value,
        )
        for index, value in enumerate(candidate_values, 1)
    )
    return controls, candidates


def _passing_confirmation():
    controls, candidates = _paired_runs()
    return build_confirmation_report(
        component=ConfirmationComponent.FORMATION,
        controls=controls,
        candidates=candidates,
    )


def _performance_evidence(*, successes: int = 30) -> PerformanceEvidence:
    rows = []
    for variant in (BenchmarkVariant.HISTORICAL_CONTROL, BenchmarkVariant.RELEASE_CANDIDATE):
        rows.extend(
            PerformanceSample(
                variant=variant,
                ordinal=index,
                warmup=True,
                duration_ms=10 + index,
                outcome=TimingOutcome.SUCCESS,
            )
            for index in range(1, 6)
        )
        rows.extend(
            PerformanceSample(
                variant=variant,
                ordinal=index + 5,
                warmup=False,
                duration_ms=100 + index,
                outcome=TimingOutcome.SUCCESS,
            )
            for index in range(1, successes + 1)
        )
    return PerformanceEvidence(
        stage=TimingStage.KIRA_COMPLETION,
        workload_sha256="9" * 64,
        environment_sha256="a" * 64,
        samples=tuple(rows),
    )


def test_confirmation_enforces_all_quality_gates_without_weighted_score():
    report = _passing_confirmation()

    assert report.verdict is ConfirmationVerdict.PASS
    assert report.primary_improved_repetitions == 2
    assert all(gate.passed for gate in report.gates)
    assert {gate.name for gate in report.gates} >= {
        "primary_aggregate_increased",
        "primary_two_of_three",
        "zero_safety_hard_fail",
        "with_ltm_beats_no_ltm",
    }


def test_confirmation_fails_regression_and_marks_missing_evidence_insufficient():
    controls, regressed = _paired_runs((0.5, 0.5, 0.5))
    failed = build_confirmation_report(
        component=ConfirmationComponent.FORMATION,
        controls=controls,
        candidates=regressed,
    )
    assert failed.verdict is ConfirmationVerdict.FAIL

    incomplete = list(regressed)
    incomplete[0] = _run(
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        number=1,
        primary=0.9,
        complete=False,
    )
    insufficient = build_confirmation_report(
        component=ConfirmationComponent.FORMATION,
        controls=controls,
        candidates=incomplete,
    )
    assert insufficient.verdict is ConfirmationVerdict.INSUFFICIENT_EVIDENCE
    assert "incomplete_run_evidence" in insufficient.unresolved_reason_codes


def test_confirmation_validates_pairing_freeze_variant_and_repetition_count():
    controls, candidates = _paired_runs()
    with pytest.raises(ValueError, match="exactly three"):
        build_confirmation_report(
            component=ConfirmationComponent.FORMATION,
            controls=controls[:2],
            candidates=candidates,
        )
    with pytest.raises(ValueError, match="historical-control"):
        build_confirmation_report(
            component=ConfirmationComponent.FORMATION,
            controls=candidates,
            candidates=candidates,
        )
    mismatched = list(candidates)
    mismatched[0] = mismatched[0].model_copy(update={"seed": 99})
    with pytest.raises(ValueError, match="not paired"):
        build_confirmation_report(
            component=ConfirmationComponent.FORMATION,
            controls=controls,
            candidates=mismatched,
        )
    drifted = list(candidates)
    drifted[0] = drifted[0].model_copy(update={"runtime_sha": "f" * 40})
    with pytest.raises(ValueError, match="frozen"):
        build_confirmation_report(
            component=ConfirmationComponent.FORMATION,
            controls=controls,
            candidates=drifted,
        )

    changed_corpus = list(candidates)
    changed_corpus[1] = changed_corpus[1].model_copy(update={"dataset_sha256": "f" * 64})
    changed_control = list(controls)
    changed_control[1] = changed_control[1].model_copy(update={"dataset_sha256": "f" * 64})
    with pytest.raises(ValueError, match="corpus must stay frozen"):
        build_confirmation_report(
            component=ConfirmationComponent.FORMATION,
            controls=changed_control,
            candidates=changed_corpus,
        )

    changed_harness = list(candidates)
    changed_harness[0] = changed_harness[0].model_copy(update={"harness_sha": "f" * 40})
    with pytest.raises(ValueError, match="identical"):
        build_confirmation_report(
            component=ConfirmationComponent.FORMATION,
            controls=controls,
            candidates=changed_harness,
        )

    duplicate_run = list(candidates)
    duplicate_run[0] = duplicate_run[0].model_copy(update={"run_id": controls[0].run_id})
    with pytest.raises(ValueError, match="distinct"):
        build_confirmation_report(
            component=ConfirmationComponent.FORMATION,
            controls=controls,
            candidates=duplicate_run,
        )


def test_reconciliation_must_account_for_and_bind_every_semantic_output():
    candidate = AuditCandidate(
        case_id="conv01:rewrite:case-1",
        subject="rewrite",
        bundle_id="conv01",
        suite=Suite.REWRITE,
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        output_sha256="1" * 64,
        judge_verdict=JudgeVerdict.PASS,
        judge_reason_code="semantic_pass",
    )
    omitted = AuditReconciliation(
        candidate_set_sha256=audit_candidate_set_sha256((candidate,)),
        cases=(),
        expansion=(),
        pending_audit_ids=(),
        insufficient_evidence_suites=(),
        dataset_revision_case_ids=(),
    )
    with pytest.raises(ValueError, match="omits"):
        release._validate_reconciliation((candidate,), omitted)

    rebound = ReconciledAuditCase(
        case_id=candidate.case_id,
        subject=candidate.subject,
        suite=candidate.suite,
        variant=candidate.variant,
        output_sha256="2" * 64,
        semantic_verdict=JudgeVerdict.PASS,
        safety_passed=True,
        overridden_by_human=False,
    )
    changed = omitted.model_copy(update={"cases": (rebound,)})
    with pytest.raises(ValueError, match="binding changed"):
        release._validate_reconciliation((candidate,), changed)


def test_safety_and_observable_task_success_are_hard_confirmation_gates():
    controls, candidates = _paired_runs()
    unsafe = list(candidates)
    unsafe[0] = _run(
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        number=1,
        primary=0.9,
        safety=("cross_user_memory",),
    )
    report = build_confirmation_report(
        component=ConfirmationComponent.FORMATION,
        controls=controls,
        candidates=unsafe,
    )
    assert report.verdict is ConfirmationVerdict.FAIL
    assert not next(gate for gate in report.gates if gate.name == "zero_safety_hard_fail").passed


def test_performance_uses_successes_only_and_keeps_human_verdict():
    report = build_performance_review(
        confirmation=_passing_confirmation(),
        evidence=_performance_evidence(),
        reviewer="reviewer-1",
        reviewed_at=_NOW,
        verdict=PerformanceReviewVerdict.ACCEPTABLE,
        rationale="No confirmed operational regression in the bounded workload.",
    )

    assert report.enough_samples
    assert report.verdict is PerformanceReviewVerdict.ACCEPTABLE
    assert all(row.successful_samples == 30 for row in report.variants)
    assert all(row.p95_ms is not None for row in report.variants)


def test_performance_needs_samples_and_only_runs_after_quality_pass():
    with pytest.raises(ValueError, match="needs_more_samples"):
        build_performance_review(
            confirmation=_passing_confirmation(),
            evidence=_performance_evidence(successes=29),
            reviewer="reviewer-1",
            reviewed_at=_NOW,
            verdict=PerformanceReviewVerdict.ACCEPTABLE,
            rationale="Not enough evidence yet.",
        )
    pending = build_performance_review(
        confirmation=_passing_confirmation(),
        evidence=_performance_evidence(successes=29),
        reviewer="reviewer-1",
        reviewed_at=_NOW,
        verdict=PerformanceReviewVerdict.NEEDS_MORE_SAMPLES,
        rationale="Collect one more successful measured sample per variant.",
    )
    assert not pending.enough_samples

    controls, candidates = _paired_runs((0.1, 0.1, 0.1))
    failed = build_confirmation_report(
        component=ConfirmationComponent.FORMATION,
        controls=controls,
        candidates=candidates,
    )
    with pytest.raises(ValueError, match="passing semantic"):
        build_performance_review(
            confirmation=failed,
            evidence=_performance_evidence(),
            reviewer="reviewer-1",
            reviewed_at=_NOW,
            verdict=PerformanceReviewVerdict.REJECT_REGRESSION,
            rationale="Quality did not pass.",
        )


def test_performance_evidence_limits_and_timezone_are_validated():
    evidence = _performance_evidence()
    duplicate = evidence.samples + (evidence.samples[0],)
    with pytest.raises(ValidationError, match="ordinals"):
        PerformanceEvidence(
            stage=evidence.stage,
            workload_sha256=evidence.workload_sha256,
            environment_sha256=evidence.environment_sha256,
            samples=duplicate,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        build_performance_review(
            confirmation=_passing_confirmation(),
            evidence=evidence,
            reviewer="reviewer-1",
            reviewed_at=datetime(2026, 9, 19),
            verdict=PerformanceReviewVerdict.ACCEPTABLE,
            rationale="Naive timestamp is invalid.",
        )


def test_promotion_binds_exact_images_cleanup_and_evidence_hashes():
    confirmation = _passing_confirmation()
    performance = build_performance_review(
        confirmation=confirmation,
        evidence=_performance_evidence(),
        reviewer="reviewer-1",
        reviewed_at=_NOW,
        verdict=PerformanceReviewVerdict.ACCEPTABLE,
        rationale="Accepted after bounded workload review.",
    )
    images = ExactImageSet(
        images={
            "control-runtime": f"registry/control-runtime@sha256:{'1' * 64}",
            "control-eval": f"registry/control-eval@sha256:{'2' * 64}",
            "candidate-runtime": f"registry/candidate-runtime@sha256:{'3' * 64}",
            "candidate-eval": f"registry/candidate-eval@sha256:{'4' * 64}",
        }
    )
    cleanup = CleanupEvidence(
        run_ids=(*confirmation.control_run_ids, *confirmation.candidate_run_ids), completed=True
    )
    report = build_promotion_report(
        confirmation=confirmation,
        performance=performance,
        images=images,
        cleanup=cleanup,
        decision=ReleaseDecision.PROMOTE_CANDIDATE,
        reviewer="reviewer-1",
        reviewed_at=_NOW,
        rationale="All quality, safety, performance and cleanup evidence passed.",
    )

    assert report.promotion_allowed
    assert report.decision is ReleaseDecision.PROMOTE_CANDIDATE

    with pytest.raises(ValueError, match="complete passing"):
        build_promotion_report(
            confirmation=confirmation,
            performance=performance.model_copy(
                update={"verdict": PerformanceReviewVerdict.NEEDS_MORE_SAMPLES}
            ),
            images=images,
            cleanup=cleanup,
            decision=ReleaseDecision.PROMOTE_CANDIDATE,
            reviewer="reviewer-1",
            reviewed_at=_NOW,
            rationale="Cannot promote incomplete evidence.",
        )


def test_image_cleanup_and_quality_models_fail_closed():
    with pytest.raises(ValidationError, match="immutable"):
        ExactImageSet(images={key: "registry/image:latest" for key in ("a", "b", "c", "d")})
    with pytest.raises(ValidationError, match="inverse"):
        CleanupEvidence(run_ids=(UUID(int=1),), completed=True, failed_resource_ids=("db-1",))
    with pytest.raises(ValidationError, match="zero-denominator"):
        QualityMetric(value=0.5, denominator=0)


def _judge(
    case_id: str,
    suite: Suite,
    output: str,
    verdict: JudgeVerdict = JudgeVerdict.PASS,
) -> SemanticJudgment:
    return SemanticJudgment(
        case_id=case_id,
        suite=suite,  # type: ignore[arg-type]
        output_sha256=output_sha256(output),
        verdict=verdict,
        reason_code="semantic_pass",
        rationale="The output satisfies the expected meaning.",
        judge=JudgeProvenance(
            provider="internal-judge",
            model="judge-model",
            prompt_sha256="b" * 64,
            response_schema_sha256="c" * 64,
        ),
    )


def _case(suite: Suite) -> EvalCase:
    common = dict(
        case_id=f"conv01:{suite.value}:case-1",
        family_id=f"conv01:family:{suite.value}",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        tags=("bundle:conv01",),
        source_row_ids=(f"conv01:{suite.value}:row-1",),
        eligibility=CaseEligibility(),
        gold=GoldSpecification(semantic_expectation="Expected behavior."),
    )
    if suite is Suite.FORMATION:
        common["inputs"] = FormationInput(
            user_id="conv01:user",
            messages=(Message(message_id="conv01:m1", role="user", content="Hà Nội"),),
        )
        common["gold"] = GoldSpecification(
            facts=(
                GoldFact(
                    gold_id="conv01:M01",
                    text="Hà Nội",
                    evidence_message_ids=("conv01:m1",),
                    attributed_to="user",
                ),
            ),
            semantic_expectation="Remember Hà Nội.",
        )
    elif suite is Suite.RETRIEVAL:
        common["inputs"] = RetrievalInput(user_id="conv01:user", current_query="Ở đâu?")
    elif suite is Suite.REWRITE:
        common["inputs"] = RewriteInput(current_query="Ở đâu?")
    else:
        common["inputs"] = CrossSessionInput(
            user_id="conv01:user",
            session_a="conv01:a",
            session_b="conv01:b",
            session_a_messages=(Message(message_id="conv01:m1", role="user", content="Hà Nội"),),
            session_b_query="Ở đâu?",
        )
    return EvalCase(**common)


def _attempt(case: EvalCase, result) -> CaseAttemptArtifact:
    payload = result.model_dump(mode="json", exclude_none=False)
    return CaseAttemptArtifact(
        case_id=case.case_id,
        suite=case.suite,
        attempt=1,
        completed_at=_NOW,
        outcome=result.outcome,
        output=payload,
        output_sha256=output_sha256(payload),
    )


def test_build_run_quality_evidence_parses_all_four_native_suites(monkeypatch, tmp_path: Path):
    cases = tuple(_case(suite) for suite in Suite)
    formation_case, retrieval_case, rewrite_case, cross_case = cases
    extraction = FormationExtractionResult(
        case_id=formation_case.case_id,
        outcome=Outcome.REVIEW_REQUIRED,
        status=FormationExecutionStatus.VALID_FACTS,
        facts=(ExtractedFact(text="Hà Nội", attributed_to="user"),),
        provider_calls=1,
        stages=(),
    )
    formation = NativeFormationCaseEvaluation(
        case_id=formation_case.case_id,
        outcome=Outcome.PASS,
        extraction=extraction,
        score=score_formation({"conv01:M01": "Hà Nội"}, ("Hà Nội",)),
    )
    retrieval_score = RetrievalScore(recall_at_3=1, reciprocal_rank=1, first_relevant_rank=1)
    retrieval = NativeRetrievalCaseEvaluation(
        case_id=retrieval_case.case_id,
        outcome=Outcome.PASS,
        gold_fixture=RetrievalCaseEvaluation(
            case_id=retrieval_case.case_id,
            mode=RetrievalMode.GOLD_FIXTURE,
            outcome=Outcome.PASS,
            score=retrieval_score,
        ),
        formation_produced=RetrievalCaseEvaluation(
            case_id=retrieval_case.case_id,
            mode=RetrievalMode.FORMATION_PRODUCED,
            outcome=Outcome.PASS,
            score=retrieval_score,
        ),
    )
    rewritten = "Hà Nội ở đâu?"
    rewrite = RewriteCaseEvaluation(
        case_id=rewrite_case.case_id,
        outcome=Outcome.REVIEW_REQUIRED,
        rewritten_query=rewritten,
        output_sha256=output_sha256(rewritten),
        constraints=ConstraintScore(passed=True),
        judgment=_judge(
            rewrite_case.case_id,
            Suite.REWRITE,
            rewritten,
            JudgeVerdict.UNCERTAIN,
        ),
    )

    def arm(condition: CrossSessionCondition, answer: str) -> CrossSessionArmEvaluation:
        return CrossSessionArmEvaluation(
            case_id=cross_case.case_id,
            condition=condition,
            outcome=Outcome.REVIEW_REQUIRED,
            rewritten_query="Hà Nội ở đâu?",
            final_answer=answer,
            rewrite_constraints=ConstraintScore(passed=True),
            final_judgment=_judge(
                cross_case.case_id,
                Suite.CROSS_SESSION,
                answer,
                JudgeVerdict.UNCERTAIN,
            ),
            task_success=TaskSuccessScore(passed=True, action_matches=True),
        )

    cross = CrossSessionCaseEvaluation(
        case_id=cross_case.case_id,
        outcome=Outcome.REVIEW_REQUIRED,
        no_ltm=arm(CrossSessionCondition.NO_LTM, "Không biết"),
        with_ltm=arm(CrossSessionCondition.WITH_LTM, "Hà Nội"),
    )
    attempts = tuple(
        _attempt(case, result)
        for case, result in zip(cases, (formation, retrieval, rewrite, cross), strict=True)
    )
    candidates = (
        AuditCandidate(
            case_id=rewrite_case.case_id,
            subject="rewrite",
            bundle_id="conv01",
            suite=Suite.REWRITE,
            variant=BenchmarkVariant.RELEASE_CANDIDATE,
            output_sha256=rewrite.judgment.output_sha256,
            judge_verdict=JudgeVerdict.UNCERTAIN,
            judge_reason_code="semantic_pass",
        ),
        AuditCandidate(
            case_id=cross_case.case_id,
            subject="final_no_ltm",
            bundle_id="conv01",
            suite=Suite.CROSS_SESSION,
            variant=BenchmarkVariant.RELEASE_CANDIDATE,
            output_sha256=cross.no_ltm.final_judgment.output_sha256,
            judge_verdict=JudgeVerdict.UNCERTAIN,
            judge_reason_code="semantic_pass",
        ),
        AuditCandidate(
            case_id=cross_case.case_id,
            subject="final_with_ltm",
            bundle_id="conv01",
            suite=Suite.CROSS_SESSION,
            variant=BenchmarkVariant.RELEASE_CANDIDATE,
            output_sha256=cross.with_ltm.final_judgment.output_sha256,
            judge_verdict=JudgeVerdict.UNCERTAIN,
            judge_reason_code="semantic_pass",
        ),
    )
    reconciliation = AuditReconciliation(
        candidate_set_sha256=audit_candidate_set_sha256(candidates),
        cases=tuple(
            ReconciledAuditCase(
                case_id=candidate.case_id,
                subject=candidate.subject,
                suite=candidate.suite,
                variant=candidate.variant,
                output_sha256=candidate.output_sha256,
                semantic_verdict=JudgeVerdict.PASS,
                safety_passed=True,
                overridden_by_human=False,
            )
            for candidate in candidates
        ),
        expansion=(),
        pending_audit_ids=(),
        insufficient_evidence_suites=(),
        dataset_revision_case_ids=(),
    )
    compilation_bytes = b"compiled\n"
    identity = SimpleNamespace(
        run_id=UUID(int=22),
        profile=Profile.INTERNAL_TEST,
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        dataset_sha256="1" * 64,
        compilation_sha256=release.sha256(compilation_bytes).hexdigest(),
        selected_case_ids=tuple(case.case_id for case in cases),
        seed=742,
        suites=tuple(Suite),
        config_sha256="2" * 64,
        provenance=SimpleNamespace(
            runtime=SimpleNamespace(sha="3" * 40),
            harness=SimpleNamespace(sha="4" * 40),
            candidate=SimpleNamespace(candidate_id="candidate-a"),
        ),
    )
    fake_manifest = SimpleNamespace(identity=identity, official=True)
    fake_store = SimpleNamespace(latest_attempts=attempts)
    fake_compilation = SimpleNamespace(cases=cases)
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        release.ArtifactRunManifest,
        "model_validate_json",
        classmethod(lambda _cls, _raw: fake_manifest),
    )
    monkeypatch.setattr(release.ArtifactStore, "resume", lambda *_args, **_kwargs: fake_store)
    monkeypatch.setattr(release, "compile_dataset", lambda *_args, **_kwargs: fake_compilation)
    monkeypatch.setattr(release, "compilation_json_bytes", lambda _compilation: compilation_bytes)
    monkeypatch.setattr(release, "build_audit_candidates", lambda **_kwargs: candidates)

    scorecard = build_run_quality_evidence(
        run_root=tmp_path,
        dataset_root=tmp_path,
        reconciliation=reconciliation,
    )

    assert scorecard.evidence_complete
    assert scorecard.metrics[QualityMetricName.FORMATION_F1].value == 1
    assert scorecard.metrics[QualityMetricName.RETRIEVAL_RECALL_AT_3].value == 1
    assert scorecard.metrics[QualityMetricName.REWRITE_SEMANTIC_PASS_RATE].value == 1
    assert scorecard.metrics[QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE].value == 1
    assert not scorecard.safety_violation_codes


def test_quality_builder_rejects_nonofficial_and_stale_audit(monkeypatch, tmp_path: Path):
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    identity = SimpleNamespace(profile=Profile.PC_OPENAI_ACCEPTANCE)
    monkeypatch.setattr(
        release.ArtifactRunManifest,
        "model_validate_json",
        classmethod(lambda _cls, _raw: SimpleNamespace(identity=identity, official=False)),
    )
    reconciliation = AuditReconciliation(
        candidate_set_sha256=_SHA,
        cases=(),
        expansion=(),
        pending_audit_ids=(),
        insufficient_evidence_suites=(),
        dataset_revision_case_ids=(),
    )
    with pytest.raises(ValueError, match="internal_test"):
        build_run_quality_evidence(
            run_root=tmp_path,
            dataset_root=tmp_path,
            reconciliation=reconciliation,
        )
