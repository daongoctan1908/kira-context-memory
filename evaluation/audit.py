"""Deterministic, targeted human-audit policy for semantic benchmark judgments."""

import hashlib
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, computed_field, field_validator, model_validator

from evaluation.models import BenchmarkVariant, EvalModel, Identifier, Sha256, Suite
from evaluation.scoring import JudgeVerdict

SemanticSuite = Literal[Suite.FORMATION, Suite.REWRITE, Suite.CROSS_SESSION]


class AuditTrigger(StrEnum):
    UNCERTAIN = "uncertain"
    DETERMINISTIC_CONFLICT = "deterministic_conflict"
    STRATIFIED_SAMPLE = "stratified_sample"
    DISAGREEMENT_EXPANSION = "disagreement_expansion"


class AuditDisposition(StrEnum):
    VERDICT = "verdict"
    GOLD_ERROR = "gold_error"


class AuditCandidate(EvalModel):
    case_id: Identifier
    bundle_id: Identifier
    suite: SemanticSuite
    variant: BenchmarkVariant
    output_sha256: Sha256
    judge_verdict: JudgeVerdict
    judge_reason_code: Identifier
    deterministic_verdict: Literal[JudgeVerdict.PASS, JudgeVerdict.FAIL] | None = None
    safety_passed: bool = True


class AuditPolicy(EvalModel):
    seed: int = Field(ge=0, le=2**63 - 1, strict=True)
    sample_rate: float = Field(default=0.10, gt=0, le=1, allow_inf_nan=False)
    minimum_sample_when_available: int = Field(default=5, ge=1, strict=True)


class AuditSelection(EvalModel):
    case_id: Identifier
    bundle_id: Identifier
    suite: SemanticSuite
    variant: BenchmarkVariant
    output_sha256: Sha256
    judge_verdict: JudgeVerdict
    judge_reason_code: Identifier
    safety_passed: bool
    triggers: tuple[AuditTrigger, ...] = Field(min_length=1)

    @field_validator("triggers")
    @classmethod
    def triggers_are_unique(cls, value: tuple[AuditTrigger, ...]) -> tuple[AuditTrigger, ...]:
        if len(value) != len(set(value)):
            raise ValueError("audit triggers must be unique")
        return value


class AuditBatch(EvalModel):
    schema_version: Literal[1] = 1
    policy: AuditPolicy
    candidate_set_sha256: Sha256
    selections: tuple[AuditSelection, ...]


class HumanAuditDecision(EvalModel):
    case_id: Identifier
    output_sha256: Sha256
    reviewer: Identifier
    reviewed_at: datetime
    disposition: AuditDisposition = AuditDisposition.VERDICT
    human_verdict: Literal[JudgeVerdict.PASS, JudgeVerdict.FAIL] | None = None
    reason_code: Identifier
    notes: Annotated[str, StringConstraints(min_length=1, max_length=1000)]

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def disposition_matches_verdict(self) -> "HumanAuditDecision":
        if self.disposition is AuditDisposition.VERDICT and self.human_verdict is None:
            raise ValueError("audit verdict disposition requires a human verdict")
        if self.disposition is AuditDisposition.GOLD_ERROR and self.human_verdict is not None:
            raise ValueError("gold errors require dataset revision, not a semantic verdict")
        return self


class ReconciledAuditCase(EvalModel):
    case_id: Identifier
    semantic_verdict: Literal[JudgeVerdict.PASS, JudgeVerdict.FAIL]
    safety_passed: bool
    overridden_by_human: bool

    @computed_field
    @property
    def passed(self) -> bool:
        """A human semantic override can never erase a hard safety failure."""

        return self.semantic_verdict is JudgeVerdict.PASS and self.safety_passed


class AuditReconciliation(EvalModel):
    cases: tuple[ReconciledAuditCase, ...]
    expansion: tuple[AuditSelection, ...]
    pending_audit_case_ids: tuple[Identifier, ...]
    insufficient_evidence_suites: tuple[Suite, ...]
    dataset_revision_case_ids: tuple[Identifier, ...]


def _candidate_digest(candidates: Sequence[AuditCandidate]) -> str:
    rows = sorted(
        (
            candidate.case_id,
            candidate.bundle_id,
            candidate.suite.value,
            candidate.variant.value,
            candidate.output_sha256,
            candidate.judge_verdict.value,
            candidate.judge_reason_code,
            candidate.deterministic_verdict.value
            if candidate.deterministic_verdict is not None
            else "",
            str(candidate.safety_passed),
        )
        for candidate in candidates
    )
    canonical = "\n".join("\0".join(row) for row in rows).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _sample_key(candidate: AuditCandidate, seed: int) -> str:
    value = (
        f"{seed}\0{candidate.suite}\0{candidate.variant}\0{candidate.bundle_id}\0"
        f"{candidate.judge_verdict}\0{candidate.case_id}"
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selection(candidate: AuditCandidate, triggers: Sequence[AuditTrigger]) -> AuditSelection:
    return AuditSelection(
        case_id=candidate.case_id,
        bundle_id=candidate.bundle_id,
        suite=candidate.suite,
        variant=candidate.variant,
        output_sha256=candidate.output_sha256,
        judge_verdict=candidate.judge_verdict,
        judge_reason_code=candidate.judge_reason_code,
        safety_passed=candidate.safety_passed,
        triggers=tuple(sorted(set(triggers), key=lambda trigger: trigger.value)),
    )


def _stratified_sample(
    candidates: Sequence[AuditCandidate],
    *,
    policy: AuditPolicy,
) -> set[str]:
    selected: set[str] = set()
    by_suite_variant: dict[tuple[Suite, BenchmarkVariant], list[AuditCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.judge_verdict in (JudgeVerdict.PASS, JudgeVerdict.FAIL):
            by_suite_variant[(candidate.suite, candidate.variant)].append(candidate)

    for group in by_suite_variant.values():
        requested = math.ceil(len(group) * policy.sample_rate)
        if len(group) >= policy.minimum_sample_when_available:
            requested = max(requested, policy.minimum_sample_when_available)
        requested = min(requested, len(group))
        strata: dict[tuple[str, JudgeVerdict], list[AuditCandidate]] = defaultdict(list)
        for candidate in group:
            strata[(candidate.bundle_id, candidate.judge_verdict)].append(candidate)
        ordered_strata = [
            sorted(values, key=lambda item: (_sample_key(item, policy.seed), item.case_id))
            for _, values in sorted(strata.items(), key=lambda item: (item[0][0], item[0][1]))
        ]
        offset = 0
        while len(selected.intersection(candidate.case_id for candidate in group)) < requested:
            progressed = False
            for stratum in ordered_strata:
                if offset < len(stratum):
                    selected.add(stratum[offset].case_id)
                    progressed = True
                    if (
                        len(selected.intersection(candidate.case_id for candidate in group))
                        >= requested
                    ):
                        break
            if not progressed:
                break
            offset += 1
    return selected


def select_audit_batch(
    candidates: Sequence[AuditCandidate],
    *,
    policy: AuditPolicy,
) -> AuditBatch:
    by_id = {candidate.case_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("audit candidate case IDs must be unique")

    triggers: dict[str, list[AuditTrigger]] = defaultdict(list)
    for candidate in candidates:
        if candidate.judge_verdict is JudgeVerdict.UNCERTAIN:
            triggers[candidate.case_id].append(AuditTrigger.UNCERTAIN)
        if (
            candidate.deterministic_verdict is not None
            and candidate.deterministic_verdict is not candidate.judge_verdict
        ):
            triggers[candidate.case_id].append(AuditTrigger.DETERMINISTIC_CONFLICT)
    for case_id in _stratified_sample(candidates, policy=policy):
        triggers[case_id].append(AuditTrigger.STRATIFIED_SAMPLE)

    selections = tuple(
        _selection(by_id[case_id], case_triggers)
        for case_id, case_triggers in sorted(triggers.items())
    )
    return AuditBatch(
        policy=policy,
        candidate_set_sha256=_candidate_digest(candidates),
        selections=selections,
    )


def reconcile_audits(
    candidates: Sequence[AuditCandidate],
    batch: AuditBatch,
    decisions: Sequence[HumanAuditDecision],
) -> AuditReconciliation:
    by_candidate = {candidate.case_id: candidate for candidate in candidates}
    by_selection = {selection.case_id: selection for selection in batch.selections}
    by_decision = {decision.case_id: decision for decision in decisions}
    if len(by_candidate) != len(candidates) or len(by_decision) != len(decisions):
        raise ValueError("candidate and audit decision case IDs must be unique")
    if batch.candidate_set_sha256 != _candidate_digest(candidates):
        raise ValueError("audit batch does not belong to this candidate set")

    for case_id, decision in by_decision.items():
        selection = by_selection.get(case_id)
        if selection is None:
            raise ValueError("human decision references a case outside the audit batch")
        if decision.output_sha256 != selection.output_sha256:
            raise ValueError("human decision is stale for the current output hash")

    expansion_keys: set[tuple[Suite, str]] = set()
    insufficient: set[Suite] = set()
    dataset_revision: set[str] = set()
    reconciled: list[ReconciledAuditCase] = []
    for candidate in candidates:
        decision = by_decision.get(candidate.case_id)
        if decision and decision.disposition is AuditDisposition.GOLD_ERROR:
            dataset_revision.add(candidate.case_id)
            insufficient.add(candidate.suite)
            continue

        human_verdict = decision.human_verdict if decision else None
        if human_verdict is not None:
            disagrees = human_verdict is not candidate.judge_verdict
            selection = by_selection[candidate.case_id]
            if disagrees and AuditTrigger.DISAGREEMENT_EXPANSION in selection.triggers:
                insufficient.add(candidate.suite)
            elif disagrees:
                expansion_keys.add((candidate.suite, candidate.judge_reason_code))
            semantic = human_verdict
        else:
            semantic = candidate.judge_verdict

        if semantic is JudgeVerdict.UNCERTAIN:
            continue
        reconciled.append(
            ReconciledAuditCase(
                case_id=candidate.case_id,
                semantic_verdict=semantic,
                safety_passed=candidate.safety_passed,
                overridden_by_human=human_verdict is not None
                and human_verdict is not candidate.judge_verdict,
            )
        )

    expansion = tuple(
        _selection(candidate, (AuditTrigger.DISAGREEMENT_EXPANSION,))
        for candidate in sorted(candidates, key=lambda item: item.case_id)
        if (candidate.suite, candidate.judge_reason_code) in expansion_keys
        and candidate.case_id not in by_selection
    )
    pending = {
        *(case_id for case_id in by_selection if case_id not in by_decision),
        *(selection.case_id for selection in expansion),
    }
    return AuditReconciliation(
        cases=tuple(case for case in reconciled if case.case_id not in pending),
        expansion=expansion,
        pending_audit_case_ids=tuple(sorted(pending)),
        insufficient_evidence_suites=tuple(sorted(insufficient, key=lambda suite: suite.value)),
        dataset_revision_case_ids=tuple(sorted(dataset_revision)),
    )
