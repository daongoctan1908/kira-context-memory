"""Minimal formation reporting and prompt/config candidate declarations.

The report contains only the locked formation headline metrics plus execution, safety and review
counts. Candidate declarations are evaluation overlays: they hash already-rendered values and
never mutate the production prompt or runtime configuration.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from evaluation.artifacts import MetricArtifact
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    BenchmarkVariant,
    EvalModel,
    Identifier,
    Outcome,
    Sha256,
)
from evaluation.scoring import FormationScore

CandidateRuntimeScope = Literal["prompt", "config"]
DeclaredDifference = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class FormationCaseReportInput(EvalModel):
    case_id: Identifier
    bundle_id: Identifier
    family_id: Identifier
    outcome: Outcome
    score: FormationScore | None = None
    reason_codes: tuple[Identifier, ...] = ()
    safety_violation_codes: tuple[Identifier, ...] = ()
    audit_required: bool = False
    audit_completed: bool = False

    @model_validator(mode="after")
    def case_state_is_consistent(self) -> "FormationCaseReportInput":
        quality_outcomes = {Outcome.PASS, Outcome.FAIL, Outcome.REVIEW_REQUIRED}
        if (self.outcome in quality_outcomes) != (self.score is not None):
            raise ValueError("formation quality outcomes and scores must appear together")
        if self.audit_completed and not self.audit_required:
            raise ValueError("completed audit must have been required")
        for values in (self.reason_codes, self.safety_violation_codes):
            if len(values) != len(set(values)):
                raise ValueError("formation case codes must be unique")
        return self


class FormationHeadline(EvalModel):
    precision: MetricArtifact
    recall: MetricArtifact
    f1: MetricArtifact
    safety_failure_cases: int = Field(ge=0, strict=True)
    dependency_error_cases: int = Field(ge=0, strict=True)
    protocol_error_cases: int = Field(ge=0, strict=True)
    uncertain_cases: int = Field(ge=0, strict=True)
    audit_coverage: MetricArtifact


class FormationFailureGroup(EvalModel):
    bundle_id: Identifier
    family_id: Identifier
    case_ids: tuple[Identifier, ...] = Field(min_length=1)
    reason_counts: dict[Identifier, int] = Field(min_length=1)

    @model_validator(mode="after")
    def failure_group_is_consistent(self) -> "FormationFailureGroup":
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("formation failure group case IDs must be unique")
        if any(count < 1 for count in self.reason_counts.values()):
            raise ValueError("formation failure reasons must have positive counts")
        return self


class FormationReport(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    run_id: UUID
    variant: BenchmarkVariant
    candidate_id: Identifier | None = None
    eligible_cases: int = Field(ge=0, strict=True)
    attempted_cases: int = Field(ge=0, strict=True)
    scored_cases: int = Field(ge=0, strict=True)
    headline: FormationHeadline
    failure_appendix: tuple[FormationFailureGroup, ...] = ()

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "FormationReport":
        if self.attempted_cases > self.eligible_cases or self.scored_cases > self.attempted_cases:
            raise ValueError("formation report counts are inconsistent")
        return self


class FormationEvaluationContract(EvalModel):
    benchmark_contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    scorer_sha256: Sha256
    judge_prompt_sha256: Sha256
    judge_schema_sha256: Sha256


class FormationCandidate(EvalModel):
    candidate_id: Identifier
    rendered_prompt_sha256: Sha256
    rendered_config_sha256: Sha256
    declared_differences: tuple[DeclaredDifference, ...] = Field(min_length=1)
    runtime_scope: tuple[CandidateRuntimeScope, ...] = Field(min_length=1, max_length=2)
    evaluation_contract: FormationEvaluationContract

    @field_validator("runtime_scope")
    @classmethod
    def runtime_scope_is_unique(
        cls,
        value: tuple[CandidateRuntimeScope, ...],
    ) -> tuple[CandidateRuntimeScope, ...]:
        if len(value) != len(set(value)):
            raise ValueError("candidate runtime scope must be unique")
        return value


class FormationCandidateRegistry(EvalModel):
    schema_version: Literal[1] = 1
    control_rendered_prompt_sha256: Sha256
    control_rendered_config_sha256: Sha256
    evaluation_contract: FormationEvaluationContract
    candidates: tuple[FormationCandidate, ...] = Field(max_length=2)

    @model_validator(mode="after")
    def candidate_declarations_are_consistent(self) -> "FormationCandidateRegistry":
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("formation candidate IDs must be unique")
        for candidate in self.candidates:
            if candidate.evaluation_contract != self.evaluation_contract:
                raise ValueError("formation candidates must share one scorer/judge contract")
            prompt_changed = candidate.rendered_prompt_sha256 != self.control_rendered_prompt_sha256
            config_changed = candidate.rendered_config_sha256 != self.control_rendered_config_sha256
            if prompt_changed != ("prompt" in candidate.runtime_scope):
                raise ValueError("candidate prompt hash does not match declared runtime scope")
            if config_changed != ("config" in candidate.runtime_scope):
                raise ValueError("candidate config hash does not match declared runtime scope")
        return self


def _ratio_metric(name: str, numerator: int, denominator: int) -> MetricArtifact:
    return MetricArtifact(
        name=name,
        value=(numerator / denominator if denominator else None),
        numerator=numerator,
        denominator=denominator,
    )


def _case_failure_reasons(case: FormationCaseReportInput) -> tuple[str, ...]:
    reasons = list(case.reason_codes)
    reasons.extend(case.safety_violation_codes)
    if case.score is not None:
        if not case.score.complete:
            reasons.append("semantic_uncertain")
        elif (case.score.false_positive or 0) or (case.score.false_negative or 0):
            reasons.append("formation_quality_mismatch")
    return tuple(dict.fromkeys(reasons))


def build_formation_report(
    *,
    run_id: UUID,
    variant: BenchmarkVariant,
    cases: Sequence[FormationCaseReportInput],
    candidate_id: str | None = None,
) -> FormationReport:
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("formation report case IDs must be unique")

    complete_scores = [
        case.score for case in cases if case.score is not None and case.score.complete
    ]
    true_positive = sum(score.true_positive for score in complete_scores)
    false_positive = sum(score.false_positive or 0 for score in complete_scores)
    false_negative = sum(score.false_negative or 0 for score in complete_scores)
    audit_required = sum(case.audit_required for case in cases)
    audit_completed = sum(case.audit_completed for case in cases)
    headline = FormationHeadline(
        precision=_ratio_metric(
            "formation_precision",
            true_positive,
            true_positive + false_positive,
        ),
        recall=_ratio_metric(
            "formation_recall",
            true_positive,
            true_positive + false_negative,
        ),
        f1=_ratio_metric(
            "formation_f1",
            2 * true_positive,
            2 * true_positive + false_positive + false_negative,
        ),
        safety_failure_cases=sum(bool(case.safety_violation_codes) for case in cases),
        dependency_error_cases=sum(case.outcome is Outcome.DEPENDENCY_ERROR for case in cases),
        protocol_error_cases=sum(case.outcome is Outcome.PROTOCOL_ERROR for case in cases),
        uncertain_cases=sum(case.score is not None and not case.score.complete for case in cases),
        audit_coverage=_ratio_metric("formation_audit_coverage", audit_completed, audit_required),
    )

    grouped: dict[tuple[str, str], list[tuple[str, tuple[str, ...]]]] = defaultdict(list)
    for case in cases:
        reasons = _case_failure_reasons(case)
        if reasons:
            grouped[(case.bundle_id, case.family_id)].append((case.case_id, reasons))
    appendix: list[FormationFailureGroup] = []
    for (bundle_id, family_id), rows in sorted(grouped.items()):
        reason_counts = Counter(reason for _, reasons in rows for reason in reasons)
        appendix.append(
            FormationFailureGroup(
                bundle_id=bundle_id,
                family_id=family_id,
                case_ids=tuple(sorted(case_id for case_id, _ in rows)),
                reason_counts=dict(sorted(reason_counts.items())),
            )
        )

    return FormationReport(
        run_id=run_id,
        variant=variant,
        candidate_id=candidate_id,
        eligible_cases=len(cases),
        attempted_cases=sum(case.outcome is not Outcome.NOT_RUN for case in cases),
        scored_cases=len(complete_scores),
        headline=headline,
        failure_appendix=tuple(appendix),
    )


def render_formation_report_markdown(report: FormationReport) -> str:
    def metric(metric_value: MetricArtifact) -> str:
        value = "N/A" if metric_value.value is None else f"{metric_value.value:.4f}"
        return f"{value} ({metric_value.numerator}/{metric_value.denominator})"

    lines = [
        "# Formation report",
        "",
        f"- Run: `{report.run_id}`",
        f"- Variant: `{report.variant.value}`",
        f"- Candidate: `{report.candidate_id or 'control'}`",
        f"- Cases: eligible={report.eligible_cases}, attempted={report.attempted_cases}, "
        f"scored={report.scored_cases}",
        "",
        "| Headline | Value |",
        "|---|---:|",
        f"| Precision | {metric(report.headline.precision)} |",
        f"| Recall | {metric(report.headline.recall)} |",
        f"| F1 | {metric(report.headline.f1)} |",
        f"| Safety failure cases | {report.headline.safety_failure_cases} |",
        f"| Dependency error cases | {report.headline.dependency_error_cases} |",
        f"| Protocol error cases | {report.headline.protocol_error_cases} |",
        f"| Uncertain cases | {report.headline.uncertain_cases} |",
        f"| Audit coverage | {metric(report.headline.audit_coverage)} |",
        "",
        "## Failure appendix",
        "",
    ]
    if not report.failure_appendix:
        lines.append("No formation failures.")
    else:
        for group in report.failure_appendix:
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in group.reason_counts.items()
            )
            lines.append(
                f"- `{group.bundle_id}` / `{group.family_id}`: "
                f"cases={len(group.case_ids)}; {reasons}"
            )
    return "\n".join(lines) + "\n"


def rendered_prompt_sha256(rendered_prompt: str) -> str:
    if not rendered_prompt.strip():
        raise ValueError("rendered candidate prompt must not be blank")
    return hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest()


def rendered_config_sha256(rendered_config: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            rendered_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("rendered candidate config must be canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def declare_formation_candidate(
    *,
    candidate_id: str,
    rendered_prompt: str,
    rendered_config: Mapping[str, object],
    declared_differences: tuple[str, ...],
    runtime_scope: tuple[CandidateRuntimeScope, ...],
    evaluation_contract: FormationEvaluationContract,
) -> FormationCandidate:
    return FormationCandidate(
        candidate_id=candidate_id,
        rendered_prompt_sha256=rendered_prompt_sha256(rendered_prompt),
        rendered_config_sha256=rendered_config_sha256(rendered_config),
        declared_differences=declared_differences,
        runtime_scope=runtime_scope,
        evaluation_contract=evaluation_contract,
    )
