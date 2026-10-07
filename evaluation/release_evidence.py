"""Offline release evidence for official paired benchmark confirmation.

This module deliberately consumes benchmark artifacts rather than telemetry.  It contains no
provider calls and cannot promote a candidate when audit, quality, safety, performance, image or
cleanup evidence is incomplete.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from evaluation.artifacts import ArtifactRunManifest, ArtifactStore
from evaluation.audit import (
    AuditCandidate,
    AuditIdentifier,
    AuditReconciliation,
    audit_candidate_set_sha256,
)
from evaluation.audit_candidates import build_audit_candidates
from evaluation.compiler import compilation_json_bytes, compile_dataset
from evaluation.cross_session import CrossSessionCaseEvaluation
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    BenchmarkVariant,
    EvalCase,
    EvalModel,
    Identifier,
    NonEmpty,
    Outcome,
    PerformanceReviewVerdict,
    Profile,
    Sha256,
    Suite,
)
from evaluation.native_executor import (
    NativeFormationCaseEvaluation,
    NativeRetrievalCaseEvaluation,
)
from evaluation.rewrite import RewriteCaseEvaluation
from evaluation.scoring import JudgeVerdict
from evaluation.timing import TimingOutcome, TimingStage, nearest_rank


class QualityMetricName(StrEnum):
    FORMATION_PRECISION = "formation_precision"
    FORMATION_RECALL = "formation_recall"
    FORMATION_F1 = "formation_f1"
    RETRIEVAL_RECALL_AT_3 = "retrieval_recall_at_3"
    RETRIEVAL_MRR_AT_10 = "retrieval_mrr_at_10"
    REWRITE_CONSTRAINT_PASS_RATE = "rewrite_constraint_pass_rate"
    REWRITE_SEMANTIC_PASS_RATE = "rewrite_semantic_pass_rate"
    FINAL_QA_SEMANTIC_PASS_RATE = "final_qa_semantic_pass_rate"
    FINAL_QA_TASK_SUCCESS_RATE = "final_qa_task_success_rate"
    NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE = "no_ltm_final_qa_semantic_pass_rate"
    NO_LTM_TASK_SUCCESS_RATE = "no_ltm_task_success_rate"


class ConfirmationComponent(StrEnum):
    FORMATION = "formation"
    RETRIEVAL = "retrieval"
    REWRITE = "rewrite"
    FINAL_QA = "final_qa"


class ConfirmationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReleaseDecision(StrEnum):
    PROMOTE_CANDIDATE = "promote_candidate"
    KEEP_CONTROL = "keep_control"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class QualityMetric(EvalModel):
    value: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    denominator: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def value_matches_denominator(self) -> QualityMetric:
        if (self.denominator == 0) != (self.value is None):
            raise ValueError("zero-denominator quality metrics must be N/A and only those are N/A")
        return self


class FamilyQuality(EvalModel):
    family_id: Identifier
    metrics: dict[QualityMetricName, QualityMetric]


class RunQualityEvidence(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    run_id: UUID
    variant: Literal[
        BenchmarkVariant.HISTORICAL_CONTROL,
        BenchmarkVariant.RELEASE_CANDIDATE,
    ]
    candidate_id: Identifier | None = None
    dataset_sha256: Sha256
    compilation_sha256: Sha256
    selected_case_ids_sha256: Sha256
    seed: int = Field(ge=0, le=2**63 - 1, strict=True)
    runtime_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    harness_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    config_sha256: Sha256
    formation_scoring_contract: (
        Literal["formation-closed-world-v1", "formation-open-world-v2"] | None
    ) = "formation-closed-world-v1"
    metrics: dict[QualityMetricName, QualityMetric]
    families: tuple[FamilyQuality, ...]
    safety_violation_codes: tuple[Identifier, ...] = ()
    pending_audit_ids: tuple[AuditIdentifier, ...] = ()
    unresolved_reason_codes: tuple[Identifier, ...] = ()
    evidence_complete: bool

    @model_validator(mode="after")
    def variant_and_candidate_match(self) -> RunQualityEvidence:
        if (self.variant is BenchmarkVariant.RELEASE_CANDIDATE) != (self.candidate_id is not None):
            raise ValueError("only release-candidate quality evidence has a candidate ID")
        if self.evidence_complete == bool(self.unresolved_reason_codes):
            raise ValueError("evidence completeness must be the inverse of unresolved reasons")
        family_ids = [family.family_id for family in self.families]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("quality evidence family IDs must be unique")
        return self


class ConfirmationGate(EvalModel):
    name: Identifier
    passed: bool
    detail: NonEmpty


class OfficialConfirmationReport(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    component: ConfirmationComponent
    control_run_ids: tuple[UUID, UUID, UUID]
    candidate_run_ids: tuple[UUID, UUID, UUID]
    candidate_id: Identifier
    primary_metric: QualityMetricName
    primary_control_mean: float
    primary_candidate_mean: float
    primary_improved_repetitions: int = Field(ge=0, le=3, strict=True)
    gates: tuple[ConfirmationGate, ...]
    verdict: ConfirmationVerdict
    unresolved_reason_codes: tuple[Identifier, ...] = ()


class PerformanceSample(EvalModel):
    variant: Literal[
        BenchmarkVariant.HISTORICAL_CONTROL,
        BenchmarkVariant.RELEASE_CANDIDATE,
    ]
    ordinal: int = Field(ge=1, strict=True)
    warmup: bool
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    outcome: TimingOutcome
    sdk_retry_count: int = Field(default=0, ge=0, strict=True)
    worker_retry_count: int = Field(default=0, ge=0, strict=True)


class PerformanceEvidence(EvalModel):
    schema_version: Literal[1] = 1
    stage: TimingStage
    workload_sha256: Sha256
    environment_sha256: Sha256
    samples: tuple[PerformanceSample, ...]

    @model_validator(mode="after")
    def samples_are_bounded_and_unique(self) -> PerformanceEvidence:
        keys = [(sample.variant, sample.ordinal) for sample in self.samples]
        if len(keys) != len(set(keys)):
            raise ValueError("performance sample ordinals must be unique per variant")
        for variant in (BenchmarkVariant.HISTORICAL_CONTROL, BenchmarkVariant.RELEASE_CANDIDATE):
            rows = [sample for sample in self.samples if sample.variant is variant]
            if sum(sample.warmup for sample in rows) < 5:
                raise ValueError("performance evidence requires at least five warmups per variant")
            if sum(not sample.warmup for sample in rows) > 40:
                raise ValueError(
                    "performance evidence allows at most 40 measured attempts per variant"
                )
        return self


class VariantPerformance(EvalModel):
    variant: Literal[
        BenchmarkVariant.HISTORICAL_CONTROL,
        BenchmarkVariant.RELEASE_CANDIDATE,
    ]
    measured_attempts: int = Field(ge=0, le=40, strict=True)
    successful_samples: int = Field(ge=0, le=30, strict=True)
    timeout_attempts: int = Field(ge=0, strict=True)
    error_attempts: int = Field(ge=0, strict=True)
    p50_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    p95_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class PerformanceReview(EvalModel):
    schema_version: Literal[1] = 1
    confirmation_sha256: Sha256
    evidence_sha256: Sha256
    stage: TimingStage
    variants: tuple[VariantPerformance, VariantPerformance]
    reviewer: Identifier
    reviewed_at: datetime
    verdict: PerformanceReviewVerdict
    rationale: NonEmpty
    enough_samples: bool

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("performance review timestamp must be timezone-aware")
        return value


class ExactImageSet(EvalModel):
    images: dict[Identifier, NonEmpty] = Field(min_length=4)

    @field_validator("images")
    @classmethod
    def images_are_immutable(cls, value: dict[str, str]) -> dict[str, str]:
        if any("@sha256:" not in reference for reference in value.values()):
            raise ValueError("release images must use immutable sha256 digest references")
        return value


class CleanupEvidence(EvalModel):
    run_ids: tuple[UUID, ...] = Field(min_length=1)
    completed: bool
    failed_resource_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def completion_matches_failures(self) -> CleanupEvidence:
        if self.completed == bool(self.failed_resource_ids):
            raise ValueError("cleanup completion must be the inverse of failed resources")
        return self


class PromotionReport(EvalModel):
    schema_version: Literal[1] = 1
    decision: ReleaseDecision
    candidate_id: Identifier
    confirmation_sha256: Sha256
    performance_sha256: Sha256
    image_set_sha256: Sha256
    cleanup_sha256: Sha256
    reviewer: Identifier
    reviewed_at: datetime
    rationale: NonEmpty
    promotion_allowed: bool

    @field_validator("reviewed_at")
    @classmethod
    def promotion_reviewed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("promotion timestamp must be timezone-aware")
        return value


def _digest_model(model: EvalModel) -> str:
    return sha256(model.model_dump_json(exclude_none=False).encode("utf-8")).hexdigest()


def _ids_digest(values: Sequence[str]) -> str:
    return sha256("\n".join(values).encode("utf-8")).hexdigest()


def _mean(values: Sequence[float]) -> QualityMetric:
    return QualityMetric(
        value=(sum(values) / len(values) if values else None),
        denominator=len(values),
    )


def is_diagnostic_tier(case: EvalCase) -> bool:
    """True when the source QA row is diagnostic_history, never acceptance-scored.

    The dataset marks known-corrupt or archival observed answers as
    diagnostic_history (Q_SINGLE_HOP_015 precedent). Their semantic verdicts are
    still judged and reported per case for debugging, but they must not move
    official acceptance aggregates whose denominators define hard gates.
    """

    return "tier:diagnostic_history" in case.tags


def is_acceptance_case(case: EvalCase) -> bool:
    """Official aggregate membership: eligible AND not a diagnostic-history case."""

    return case.eligibility.status == "eligible" and not is_diagnostic_tier(case)


def _formation_metrics(
    scores: Sequence[NativeFormationCaseEvaluation],
) -> dict[QualityMetricName, QualityMetric]:
    complete = [item.score for item in scores if item.score is not None and item.score.complete]
    if len({item.scoring_contract for item in complete}) > 1:
        raise ValueError("formation quality evidence mixes scoring contracts")
    tp = sum(item.true_positive for item in complete)
    valid_extra = sum(item.valid_extra for item in complete)
    fp = sum(item.false_positive or 0 for item in complete)
    fn = sum(item.false_negative or 0 for item in complete)

    def ratio(numerator: int, denominator: int) -> QualityMetric:
        return QualityMetric(
            value=numerator / denominator if denominator else None, denominator=denominator
        )

    precision = ratio(tp + valid_extra, tp + valid_extra + fp)
    recall = ratio(tp, tp + fn)
    if valid_extra:
        f1_denominator = tp + valid_extra + fp + tp + fn if tp + fn else 0
        f1_value = (
            2 * precision.value * recall.value / (precision.value + recall.value)
            if precision.value is not None
            and recall.value is not None
            and precision.value + recall.value > 0
            else (0.0 if f1_denominator else None)
        )
        f1 = QualityMetric(value=f1_value, denominator=f1_denominator)
    else:
        f1 = ratio(2 * tp, 2 * tp + fp + fn)
    return {
        QualityMetricName.FORMATION_PRECISION: precision,
        QualityMetricName.FORMATION_RECALL: recall,
        QualityMetricName.FORMATION_F1: f1,
    }


def _semantic_pass(
    judgment,
    overrides: Mapping[tuple[str, str], JudgeVerdict],
    case_id: str,
    subject: str,
) -> float | None:
    if judgment is None:
        return None
    verdict = overrides.get((case_id, subject), judgment.verdict)
    if verdict is JudgeVerdict.UNCERTAIN:
        return None
    return float(verdict is JudgeVerdict.PASS)


def _validate_reconciliation(
    candidates: Sequence[AuditCandidate],
    reconciliation: AuditReconciliation,
) -> None:
    by_id = {f"{candidate.case_id}:{candidate.subject}": candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("quality evidence audit candidates are not unique")
    reconciled_ids: set[str] = set()
    for item in reconciliation.cases:
        audit_id = f"{item.case_id}:{item.subject}"
        candidate = by_id.get(audit_id)
        if candidate is None:
            raise ValueError("audit reconciliation contains an unknown semantic output")
        if (
            item.suite is not candidate.suite
            or item.variant is not candidate.variant
            or item.output_sha256 != candidate.output_sha256
            or item.safety_passed != candidate.safety_passed
        ):
            raise ValueError("audit reconciliation semantic output binding changed")
        reconciled_ids.add(audit_id)

    pending = set(reconciliation.pending_audit_ids)
    if not pending.issubset(by_id):
        raise ValueError("audit reconciliation has unknown pending IDs")
    expansion = {f"{item.case_id}:{item.subject}" for item in reconciliation.expansion}
    if not expansion.issubset(pending):
        raise ValueError("audit reconciliation expansion is not pending")
    dataset_revision = set(reconciliation.dataset_revision_case_ids)
    if not dataset_revision.issubset({candidate.case_id for candidate in candidates}):
        raise ValueError("audit reconciliation has unknown dataset-revision cases")
    accounted = reconciled_ids | pending
    missing = {
        audit_id
        for audit_id, candidate in by_id.items()
        if audit_id not in accounted and candidate.case_id not in dataset_revision
    }
    if missing:
        raise ValueError("audit reconciliation omits semantic outputs")


def build_run_quality_evidence(
    *,
    run_root: Path,
    dataset_root: Path,
    reconciliation: AuditReconciliation,
) -> RunQualityEvidence:
    manifest = ArtifactRunManifest.model_validate_json(
        (run_root / "manifest.json").read_text(encoding="utf-8")
    )
    identity = manifest.identity
    if identity.profile is not Profile.INTERNAL_TEST or not manifest.official:
        raise ValueError("official quality evidence requires an internal_test run")
    if identity.variant not in {
        BenchmarkVariant.HISTORICAL_CONTROL,
        BenchmarkVariant.RELEASE_CANDIDATE,
    }:
        raise ValueError("official quality evidence requires control or release-candidate runtime")
    store = ArtifactStore.resume(run_root, expected_identity=identity)
    compilation = compile_dataset(dataset_root, seed=identity.seed)
    ordered = tuple(
        case for suite in identity.suites for case in compilation.cases if case.suite is suite
    )
    if sha256(compilation_json_bytes(compilation)).hexdigest() != identity.compilation_sha256:
        raise ValueError("quality-evidence compilation differs from the run")
    if tuple(case.case_id for case in ordered) != identity.selected_case_ids:
        raise ValueError("quality-evidence case order differs from the run")

    audit_candidates = build_audit_candidates(run_root=run_root, dataset_root=dataset_root)
    if audit_candidate_set_sha256(audit_candidates) != reconciliation.candidate_set_sha256:
        raise ValueError("audit reconciliation belongs to another candidate set")

    _validate_reconciliation(audit_candidates, reconciliation)

    if any(case.variant is not identity.variant for case in reconciliation.cases):
        raise ValueError("audit reconciliation variant differs from the run")
    overrides = {
        (case.case_id, case.subject): case.semantic_verdict for case in reconciliation.cases
    }
    unresolved = {"pending_human_audit"} if reconciliation.pending_audit_ids else set()
    unresolved.update(
        f"audit_{suite.value}_insufficient" for suite in reconciliation.insufficient_evidence_suites
    )
    unresolved.update("dataset_revision_required" for _ in reconciliation.dataset_revision_case_ids)
    if any(
        case.overridden_by_human and case.suite is Suite.FORMATION for case in reconciliation.cases
    ):
        unresolved.add("formation_audit_override_requires_rescore")
    if any(
        case.overridden_by_human
        and case.suite is Suite.CROSS_SESSION
        and case.subject.startswith("rewrite_")
        for case in reconciliation.cases
    ):
        unresolved.add("cross_session_rewrite_audit_disagreement")
    latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
    formation: list[NativeFormationCaseEvaluation] = []
    retrieval_recall: list[float] = []
    retrieval_mrr: list[float] = []
    rewrite_constraints: list[float] = []
    rewrite_semantic: list[float] = []
    final_semantic: list[float] = []
    final_task: list[float] = []
    no_ltm_semantic: list[float] = []
    no_ltm_task: list[float] = []
    safety: set[str] = set()
    family_rows: dict[str, list[tuple[Suite, object]]] = defaultdict(list)

    for case in ordered:
        if case.eligibility.status == "blocked":
            continue
        attempt = latest.get(case.case_id)
        if attempt is None:
            unresolved.add("missing_case_attempt")
            continue
        if attempt.outcome in {
            Outcome.NOT_RUN,
            Outcome.DEPENDENCY_ERROR,
            Outcome.PROTOCOL_ERROR,
            Outcome.INSUFFICIENT_EVIDENCE,
        }:
            unresolved.add(f"{case.suite.value}_unresolved")
            continue
        if attempt.output is None:
            unresolved.add("missing_case_output")
            continue
        # Diagnostic-history cases stay fully executed and audited (outcomes,
        # safety, unresolved coverage below), but their verdicts never enter an
        # official quality aggregate; only acceptance-tier cases feed metrics.
        if not is_acceptance_case(case):
            if case.suite is Suite.CROSS_SESSION:
                result = CrossSessionCaseEvaluation.model_validate(attempt.output)
                if result.with_ltm is not None:
                    safety.update(result.with_ltm.safety_violation_codes)
                if result.no_ltm is not None:
                    safety.update(result.no_ltm.safety_violation_codes)
            elif case.suite is Suite.RETRIEVAL:
                result = NativeRetrievalCaseEvaluation.model_validate(attempt.output)
                if result.formation_produced:
                    safety.update(result.formation_produced.safety_violation_codes)
            continue
        if case.suite is Suite.FORMATION:
            result = NativeFormationCaseEvaluation.model_validate(attempt.output)
            if result.score is None or not result.score.complete:
                unresolved.add("formation_score_incomplete")
            formation.append(result)
        elif case.suite is Suite.RETRIEVAL:
            result = NativeRetrievalCaseEvaluation.model_validate(attempt.output)
            score = result.formation_produced.score if result.formation_produced else None
            if score is None or score.recall_at_3 is None or score.reciprocal_rank is None:
                unresolved.add("retrieval_formation_produced_unscored")
            else:
                retrieval_recall.append(score.recall_at_3)
                retrieval_mrr.append(score.reciprocal_rank)
            if result.formation_produced:
                safety.update(result.formation_produced.safety_violation_codes)
        elif case.suite is Suite.REWRITE:
            result = RewriteCaseEvaluation.model_validate(attempt.output)
            if result.constraints is not None:
                rewrite_constraints.append(float(result.constraints.passed))
            else:
                unresolved.add("rewrite_constraints_missing")
            semantic = _semantic_pass(result.judgment, overrides, case.case_id, "rewrite")
            if semantic is not None:
                rewrite_semantic.append(semantic)
            elif result.constraints is not None and result.constraints.passed:
                unresolved.add("rewrite_semantic_missing")
        else:
            result = CrossSessionCaseEvaluation.model_validate(attempt.output)
            if result.no_ltm is None or result.with_ltm is None:
                unresolved.add("cross_session_pair_missing")
            else:
                with_semantic = _semantic_pass(
                    result.with_ltm.final_judgment,
                    overrides,
                    case.case_id,
                    "final_with_ltm",
                )
                no_semantic = _semantic_pass(
                    result.no_ltm.final_judgment,
                    overrides,
                    case.case_id,
                    "final_no_ltm",
                )
                if with_semantic is not None:
                    final_semantic.append(with_semantic)
                else:
                    unresolved.add("with_ltm_final_semantic_missing")
                if no_semantic is not None:
                    no_ltm_semantic.append(no_semantic)
                else:
                    unresolved.add("no_ltm_final_semantic_missing")
                if result.with_ltm.task_success is not None:
                    final_task.append(float(result.with_ltm.task_success.passed))
                if result.no_ltm.task_success is not None:
                    no_ltm_task.append(float(result.no_ltm.task_success.passed))
                safety.update(result.with_ltm.safety_violation_codes)
                safety.update(result.no_ltm.safety_violation_codes)
        family_rows[case.family_id].append((case.suite, attempt.output))

    metrics = {
        **_formation_metrics(formation),
        QualityMetricName.RETRIEVAL_RECALL_AT_3: _mean(retrieval_recall),
        QualityMetricName.RETRIEVAL_MRR_AT_10: _mean(retrieval_mrr),
        QualityMetricName.REWRITE_CONSTRAINT_PASS_RATE: _mean(rewrite_constraints),
        QualityMetricName.REWRITE_SEMANTIC_PASS_RATE: _mean(rewrite_semantic),
        QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE: _mean(final_semantic),
        QualityMetricName.FINAL_QA_TASK_SUCCESS_RATE: _mean(final_task),
        QualityMetricName.NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE: _mean(no_ltm_semantic),
        QualityMetricName.NO_LTM_TASK_SUCCESS_RATE: _mean(no_ltm_task),
    }
    # Family evidence intentionally reuses the same deterministic parser on a closed subset.
    families: list[FamilyQuality] = []
    for family_id, rows in sorted(family_rows.items()):
        family_formation = [
            NativeFormationCaseEvaluation.model_validate(output)
            for suite, output in rows
            if suite is Suite.FORMATION
        ]
        family_metrics = _formation_metrics(family_formation)
        families.append(FamilyQuality(family_id=family_id, metrics=family_metrics))

    candidate = identity.provenance.candidate
    return RunQualityEvidence(
        run_id=identity.run_id,
        variant=identity.variant,
        candidate_id=candidate.candidate_id if candidate else None,
        dataset_sha256=identity.dataset_sha256,
        compilation_sha256=identity.compilation_sha256,
        selected_case_ids_sha256=_ids_digest(identity.selected_case_ids),
        seed=identity.seed,
        runtime_sha=identity.provenance.runtime.sha,
        harness_sha=identity.provenance.harness.sha,
        config_sha256=identity.config_sha256,
        formation_scoring_contract=next(
            (result.score.scoring_contract for result in formation if result.score is not None),
            None,
        ),
        metrics=metrics,
        families=tuple(families),
        safety_violation_codes=tuple(sorted(safety)),
        pending_audit_ids=reconciliation.pending_audit_ids,
        unresolved_reason_codes=tuple(sorted(unresolved)),
        evidence_complete=not unresolved,
    )


_PRIMARY = {
    ConfirmationComponent.FORMATION: QualityMetricName.FORMATION_F1,
    ConfirmationComponent.RETRIEVAL: QualityMetricName.RETRIEVAL_RECALL_AT_3,
    ConfirmationComponent.REWRITE: QualityMetricName.REWRITE_SEMANTIC_PASS_RATE,
    ConfirmationComponent.FINAL_QA: QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE,
}
_GUARDRAILS = {
    ConfirmationComponent.FORMATION: (
        QualityMetricName.FORMATION_PRECISION,
        QualityMetricName.FORMATION_RECALL,
    ),
    ConfirmationComponent.RETRIEVAL: (QualityMetricName.RETRIEVAL_MRR_AT_10,),
    ConfirmationComponent.REWRITE: (QualityMetricName.REWRITE_CONSTRAINT_PASS_RATE,),
    ConfirmationComponent.FINAL_QA: (QualityMetricName.FINAL_QA_TASK_SUCCESS_RATE,),
}


def _values(runs: Sequence[RunQualityEvidence], metric: QualityMetricName) -> list[float]:
    values = [run.metrics[metric].value for run in runs]
    return [value for value in values if value is not None]


def build_confirmation_report(
    *,
    component: ConfirmationComponent,
    controls: Sequence[RunQualityEvidence],
    candidates: Sequence[RunQualityEvidence],
) -> OfficialConfirmationReport:
    if len(controls) != 3 or len(candidates) != 3:
        raise ValueError("official confirmation requires exactly three paired repetitions")
    if any(run.variant is not BenchmarkVariant.HISTORICAL_CONTROL for run in controls):
        raise ValueError("confirmation controls must use the historical-control variant")
    if any(run.variant is not BenchmarkVariant.RELEASE_CANDIDATE for run in candidates):
        raise ValueError("confirmation candidates must use the release-candidate variant")
    candidate_ids = {run.candidate_id for run in candidates}
    if len(candidate_ids) != 1 or None in candidate_ids:
        raise ValueError("confirmation repetitions must use one candidate")
    run_ids = [run.run_id for run in (*controls, *candidates)]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("confirmation repetitions must use six distinct run IDs")
    for control, candidate in zip(controls, candidates, strict=True):
        if (
            control.dataset_sha256 != candidate.dataset_sha256
            or control.compilation_sha256 != candidate.compilation_sha256
            or control.selected_case_ids_sha256 != candidate.selected_case_ids_sha256
            or control.seed != candidate.seed
        ):
            raise ValueError("confirmation repetitions are not paired on corpus and seed")
    all_runs = (*controls, *candidates)
    if len({run.formation_scoring_contract for run in all_runs}) > 1:
        raise ValueError("confirmation repetitions mix formation scoring contracts")
    if (
        len({run.dataset_sha256 for run in all_runs}) != 1
        or len({run.compilation_sha256 for run in all_runs}) != 1
        or len({run.selected_case_ids_sha256 for run in all_runs}) != 1
    ):
        raise ValueError("confirmation corpus must stay frozen across all repetitions")
    if len({run.harness_sha for run in all_runs}) != 1:
        raise ValueError("confirmation harness must be identical for both variants")
    for side in (controls, candidates):
        if (
            len({run.runtime_sha for run in side}) != 1
            or len({run.config_sha256 for run in side}) != 1
        ):
            raise ValueError("each confirmation variant must stay frozen across repetitions")

    unresolved = sorted(
        {reason for run in (*controls, *candidates) for reason in run.unresolved_reason_codes}
    )
    if any(not run.evidence_complete for run in (*controls, *candidates)):
        unresolved.append("incomplete_run_evidence")
    primary = _PRIMARY[component]
    control_values = _values(controls, primary)
    candidate_values = _values(candidates, primary)
    if len(control_values) != 3 or len(candidate_values) != 3:
        unresolved.append("primary_metric_unobservable")
        control_values = control_values or [0.0]
        candidate_values = candidate_values or [0.0]
    control_mean = sum(control_values) / len(control_values)
    candidate_mean = sum(candidate_values) / len(candidate_values)
    improved = sum(
        candidate > control
        for control, candidate in zip(control_values, candidate_values, strict=True)
    )
    gates = [
        ConfirmationGate(
            name="primary_aggregate_increased",
            passed=candidate_mean > control_mean,
            detail=f"{primary.value}: control={control_mean:.6f}, candidate={candidate_mean:.6f}",
        ),
        ConfirmationGate(
            name="primary_two_of_three",
            passed=improved >= 2,
            detail=f"improved_repetitions={improved}/3",
        ),
    ]
    for metric in _GUARDRAILS[component]:
        control_guard = _values(controls, metric)
        candidate_guard = _values(candidates, metric)
        observable = len(control_guard) == len(candidate_guard) == 3
        passed = observable and sum(candidate_guard) / 3 >= sum(control_guard) / 3
        gates.append(
            ConfirmationGate(
                name=f"guardrail_{metric.value}",
                passed=passed,
                detail="observable aggregate did not decrease"
                if passed
                else "missing or decreased",
            )
        )
    with_ltm = _values(candidates, QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE)
    without_ltm = _values(candidates, QualityMetricName.NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE)
    ltm_observable = len(with_ltm) == len(without_ltm) == 3
    gates.append(
        ConfirmationGate(
            name="with_ltm_beats_no_ltm",
            passed=ltm_observable and sum(with_ltm) > sum(without_ltm),
            detail="candidate paired final-QA semantic comparison",
        )
    )
    with_task = _values(candidates, QualityMetricName.FINAL_QA_TASK_SUCCESS_RATE)
    without_task = _values(candidates, QualityMetricName.NO_LTM_TASK_SUCCESS_RATE)
    task_observable = bool(with_task or without_task)
    task_passed = not task_observable or (
        len(with_task) == len(without_task) == 3 and sum(with_task) >= sum(without_task)
    )
    gates.append(
        ConfirmationGate(
            name="task_success_not_decreased_when_observable",
            passed=task_passed,
            detail="not observable" if not task_observable else "paired task-success comparison",
        )
    )
    safety_passed = all(not run.safety_violation_codes for run in (*controls, *candidates))
    gates.append(
        ConfirmationGate(
            name="zero_safety_hard_fail",
            passed=safety_passed,
            detail="no safety violation codes" if safety_passed else "safety violation present",
        )
    )
    unresolved = tuple(sorted(set(unresolved)))
    verdict = (
        ConfirmationVerdict.INSUFFICIENT_EVIDENCE
        if unresolved
        else ConfirmationVerdict.PASS
        if all(gate.passed for gate in gates)
        else ConfirmationVerdict.FAIL
    )
    return OfficialConfirmationReport(
        component=component,
        control_run_ids=tuple(run.run_id for run in controls),  # type: ignore[arg-type]
        candidate_run_ids=tuple(run.run_id for run in candidates),  # type: ignore[arg-type]
        candidate_id=next(iter(candidate_ids)),
        primary_metric=primary,
        primary_control_mean=control_mean,
        primary_candidate_mean=candidate_mean,
        primary_improved_repetitions=improved,
        gates=tuple(gates),
        verdict=verdict,
        unresolved_reason_codes=unresolved,
    )


def build_performance_review(
    *,
    confirmation: OfficialConfirmationReport,
    evidence: PerformanceEvidence,
    reviewer: str,
    reviewed_at: datetime,
    verdict: PerformanceReviewVerdict,
    rationale: str,
) -> PerformanceReview:
    if confirmation.verdict is not ConfirmationVerdict.PASS:
        raise ValueError("performance review requires a passing semantic confirmation")
    summaries: list[VariantPerformance] = []
    enough = True
    for variant in (BenchmarkVariant.HISTORICAL_CONTROL, BenchmarkVariant.RELEASE_CANDIDATE):
        measured = sorted(
            (
                sample
                for sample in evidence.samples
                if sample.variant is variant and not sample.warmup
            ),
            key=lambda sample: sample.ordinal,
        )
        successful = [
            sample.duration_ms for sample in measured if sample.outcome is TimingOutcome.SUCCESS
        ][:30]
        enough = enough and len(successful) == 30
        summaries.append(
            VariantPerformance(
                variant=variant,
                measured_attempts=len(measured),
                successful_samples=len(successful),
                timeout_attempts=sum(
                    sample.outcome is TimingOutcome.TIMEOUT for sample in measured
                ),
                error_attempts=sum(sample.outcome is TimingOutcome.ERROR for sample in measured),
                p50_ms=nearest_rank(successful, 50) if successful else None,
                p95_ms=nearest_rank(successful, 95) if successful else None,
            )
        )
    if not enough and verdict is not PerformanceReviewVerdict.NEEDS_MORE_SAMPLES:
        raise ValueError("fewer than 30 successful samples requires needs_more_samples")
    return PerformanceReview(
        confirmation_sha256=_digest_model(confirmation),
        evidence_sha256=_digest_model(evidence),
        stage=evidence.stage,
        variants=tuple(summaries),  # type: ignore[arg-type]
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        verdict=verdict,
        rationale=rationale,
        enough_samples=enough,
    )


def build_promotion_report(
    *,
    confirmation: OfficialConfirmationReport,
    performance: PerformanceReview,
    images: ExactImageSet,
    cleanup: CleanupEvidence,
    decision: ReleaseDecision,
    reviewer: str,
    reviewed_at: datetime,
    rationale: str,
) -> PromotionReport:
    evidence_allows_promotion = (
        confirmation.verdict is ConfirmationVerdict.PASS
        and performance.confirmation_sha256 == _digest_model(confirmation)
        and performance.verdict is PerformanceReviewVerdict.ACCEPTABLE
        and performance.enough_samples
        and cleanup.completed
    )
    if decision is ReleaseDecision.PROMOTE_CANDIDATE and not evidence_allows_promotion:
        raise ValueError(
            "candidate promotion requires complete passing quality/performance/cleanup evidence"
        )
    if decision is ReleaseDecision.INSUFFICIENT_EVIDENCE and evidence_allows_promotion:
        raise ValueError("complete passing evidence cannot be labeled insufficient")
    return PromotionReport(
        decision=decision,
        candidate_id=confirmation.candidate_id,
        confirmation_sha256=_digest_model(confirmation),
        performance_sha256=_digest_model(performance),
        image_set_sha256=_digest_model(images),
        cleanup_sha256=_digest_model(cleanup),
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        rationale=rationale,
        promotion_allowed=evidence_allows_promotion,
    )


__all__ = [
    "CleanupEvidence",
    "ConfirmationComponent",
    "ConfirmationVerdict",
    "ExactImageSet",
    "OfficialConfirmationReport",
    "PerformanceEvidence",
    "PerformanceReview",
    "PerformanceSample",
    "PromotionReport",
    "QualityMetric",
    "QualityMetricName",
    "ReleaseDecision",
    "RunQualityEvidence",
    "build_confirmation_report",
    "build_performance_review",
    "build_promotion_report",
    "build_run_quality_evidence",
]
