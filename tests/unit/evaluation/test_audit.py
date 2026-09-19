"""Targeted human audit selection and reconciliation tests."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evaluation.audit import (
    AuditBatch,
    AuditCandidate,
    AuditDisposition,
    AuditPolicy,
    AuditTrigger,
    HumanAuditDecision,
    reconcile_audits,
    select_audit_batch,
)
from evaluation.models import BenchmarkVariant, Suite
from evaluation.scoring import JudgeVerdict

_HASH = "a" * 64


def _candidate(
    number: int,
    *,
    suite: Suite = Suite.REWRITE,
    variant: BenchmarkVariant = BenchmarkVariant.HISTORICAL_CONTROL,
    bundle: str = "conv01",
    verdict: JudgeVerdict = JudgeVerdict.PASS,
    deterministic: JudgeVerdict | None = None,
    reason: str = "semantic_equivalent",
    safety_passed: bool = True,
) -> AuditCandidate:
    return AuditCandidate(
        case_id=f"{bundle}:case-{number}",
        bundle_id=bundle,
        suite=suite,
        variant=variant,
        output_sha256=f"{number:064x}",
        judge_verdict=verdict,
        judge_reason_code=reason,
        deterministic_verdict=deterministic,
        safety_passed=safety_passed,
    )


def _decision(
    candidate: AuditCandidate,
    verdict: JudgeVerdict | None,
    *,
    disposition: AuditDisposition = AuditDisposition.VERDICT,
) -> HumanAuditDecision:
    return HumanAuditDecision(
        case_id=candidate.case_id,
        output_sha256=candidate.output_sha256,
        reviewer="reviewer-1",
        reviewed_at=datetime(2026, 9, 18, tzinfo=UTC),
        disposition=disposition,
        human_verdict=verdict,
        reason_code="human_review",
        notes="Reviewed against the frozen gold and visible model output.",
    )


def test_selection_is_deterministic_stratified_and_not_full_manual_review():
    candidates = [
        _candidate(
            index,
            bundle=f"conv{index % 4 + 1:02d}",
            verdict=JudgeVerdict.PASS if index % 2 else JudgeVerdict.FAIL,
        )
        for index in range(40)
    ]
    policy = AuditPolicy(seed=742)

    first = select_audit_batch(candidates, policy=policy)
    reversed_batch = select_audit_batch(list(reversed(candidates)), policy=policy)

    assert first == reversed_batch
    assert len(first.selections) == 4
    assert {selection.bundle_id for selection in first.selections}.issubset(
        {"conv01", "conv02", "conv03", "conv04"}
    )
    assert {selection.judge_verdict for selection in first.selections} == {
        JudgeVerdict.PASS,
        JudgeVerdict.FAIL,
    }
    assert all(
        selection.triggers == (AuditTrigger.STRATIFIED_SAMPLE,) for selection in first.selections
    )


def test_sample_budget_is_global_across_suite_and_variant():
    candidates = [
        *[_candidate(index) for index in range(10)],
        *[
            _candidate(
                100 + index,
                suite=Suite.CROSS_SESSION,
                variant=BenchmarkVariant.RELEASE_CANDIDATE,
            )
            for index in range(10)
        ],
    ]
    batch = select_audit_batch(candidates, policy=AuditPolicy(seed=1))

    assert len(batch.selections) == 2
    assert {selection.suite for selection in batch.selections} == {
        Suite.REWRITE,
        Suite.CROSS_SESSION,
    }
    assert {selection.variant for selection in batch.selections} == {
        BenchmarkVariant.HISTORICAL_CONTROL,
        BenchmarkVariant.RELEASE_CANDIDATE,
    }


def test_mandatory_cases_are_in_addition_to_global_sample_budget():
    candidates = [_candidate(index) for index in range(20)]
    candidates.append(_candidate(90, verdict=JudgeVerdict.UNCERTAIN))
    candidates.append(_candidate(91, verdict=JudgeVerdict.PASS, deterministic=JudgeVerdict.FAIL))

    batch = select_audit_batch(candidates, policy=AuditPolicy(seed=2))

    sampled = [item for item in batch.selections if AuditTrigger.STRATIFIED_SAMPLE in item.triggers]
    # 21 PASS/FAIL candidates => one global ceil(10%) budget of three.
    assert len(sampled) == 3
    assert {"conv01:case-90", "conv01:case-91"}.issubset(
        {item.case_id for item in batch.selections}
    )


def test_uncertain_and_deterministic_conflict_are_always_selected():
    uncertain = _candidate(90, verdict=JudgeVerdict.UNCERTAIN)
    conflict = _candidate(
        91,
        verdict=JudgeVerdict.PASS,
        deterministic=JudgeVerdict.FAIL,
    )
    batch = select_audit_batch([uncertain, conflict], policy=AuditPolicy(seed=2))
    by_id = {selection.case_id: selection for selection in batch.selections}

    assert AuditTrigger.UNCERTAIN in by_id[uncertain.case_id].triggers
    assert AuditTrigger.DETERMINISTIC_CONFLICT in by_id[conflict.case_id].triggers


def test_same_case_with_distinct_semantic_subjects_is_audited_independently():
    no_ltm = _candidate(1).model_copy(update={"subject": "final_no_ltm"})
    with_ltm = _candidate(1).model_copy(
        update={"subject": "final_with_ltm", "output_sha256": "f" * 64}
    )

    batch = select_audit_batch(
        [no_ltm, with_ltm],
        policy=AuditPolicy(seed=3, sample_rate=1.0),
    )

    assert {selection.subject for selection in batch.selections} == {
        "final_no_ltm",
        "final_with_ltm",
    }
    with pytest.raises(ValueError, match="case/subject"):
        select_audit_batch([no_ltm, no_ltm], policy=AuditPolicy(seed=3))

    with pytest.raises(ValidationError, match="unique case/subject"):
        AuditBatch(
            policy=AuditPolicy(seed=3),
            candidate_set_sha256=batch.candidate_set_sha256,
            selections=(batch.selections[0], batch.selections[0]),
        )


def test_human_override_expands_same_suite_and_reason_only():
    disagreed = _candidate(1, verdict=JudgeVerdict.PASS, reason="missed_negation")
    same_reason = _candidate(2, verdict=JudgeVerdict.PASS, reason="missed_negation")
    other_reason = _candidate(3, verdict=JudgeVerdict.PASS, reason="wrong_slot")
    candidates = [disagreed, same_reason, other_reason]
    # A 1.0 rate makes the initial batch include every case; rebuild a narrow batch to model an
    # already-selected mandatory case while keeping the production selection schema intact.
    full = select_audit_batch(candidates, policy=AuditPolicy(seed=4, sample_rate=1.0))
    narrow = full.model_copy(update={"selections": (full.selections[0],)})
    selected = candidates[[item.case_id for item in candidates].index(narrow.selections[0].case_id)]
    selected_reason = selected.judge_reason_code
    decision = _decision(selected, JudgeVerdict.FAIL)

    result = reconcile_audits(candidates, narrow, [decision])

    assert next(
        item for item in result.cases if item.case_id == selected.case_id
    ).overridden_by_human
    assert {item.case_id for item in result.expansion} == {
        item.case_id
        for item in candidates
        if item.case_id != selected.case_id and item.judge_reason_code == selected_reason
    }
    assert all(item.triggers == (AuditTrigger.DISAGREEMENT_EXPANSION,) for item in result.expansion)
    assert result.pending_audit_ids == tuple(
        sorted(f"{item.case_id}:{item.subject}" for item in result.expansion)
    )


def test_second_disagreement_marks_suite_insufficient_evidence():
    candidate = _candidate(1, verdict=JudgeVerdict.PASS)
    original = select_audit_batch([candidate], policy=AuditPolicy(seed=5, sample_rate=1.0))
    expanded_selection = original.selections[0].model_copy(
        update={"triggers": (AuditTrigger.DISAGREEMENT_EXPANSION,)}
    )
    expanded_batch = original.model_copy(update={"selections": (expanded_selection,)})

    result = reconcile_audits(
        [candidate],
        expanded_batch,
        [_decision(candidate, JudgeVerdict.FAIL)],
    )

    assert result.insufficient_evidence_suites == (Suite.REWRITE,)
    assert result.pending_audit_ids == ()


def test_review_is_output_hash_bound_and_unselected_cases_are_rejected():
    candidate = _candidate(1)
    batch = select_audit_batch([candidate], policy=AuditPolicy(seed=5, sample_rate=1.0))
    stale = _decision(candidate, JudgeVerdict.PASS).model_copy(update={"output_sha256": "f" * 64})
    with pytest.raises(ValueError, match="stale"):
        reconcile_audits([candidate], batch, [stale])

    outsider = _candidate(2)
    complete_batch = select_audit_batch(
        [candidate, outsider], policy=AuditPolicy(seed=5, sample_rate=1.0)
    )
    batch_without_outsider = complete_batch.model_copy(
        update={
            "selections": tuple(
                item for item in complete_batch.selections if item.case_id != outsider.case_id
            )
        }
    )
    with pytest.raises(ValueError, match="outside"):
        reconcile_audits(
            [candidate, outsider],
            batch_without_outsider,
            [_decision(outsider, JudgeVerdict.PASS)],
        )


def test_human_semantic_pass_cannot_override_hard_safety_failure():
    candidate = _candidate(1, verdict=JudgeVerdict.FAIL, safety_passed=False)
    batch = select_audit_batch([candidate], policy=AuditPolicy(seed=5, sample_rate=1.0))
    result = reconcile_audits([candidate], batch, [_decision(candidate, JudgeVerdict.PASS)])

    reconciled = result.cases[0]
    assert reconciled.semantic_verdict is JudgeVerdict.PASS
    assert not reconciled.safety_passed
    assert not reconciled.passed


def test_gold_error_requires_dataset_revision_and_no_case_verdict():
    candidate = _candidate(1)
    batch = select_audit_batch([candidate], policy=AuditPolicy(seed=5, sample_rate=1.0))
    decision = _decision(candidate, None, disposition=AuditDisposition.GOLD_ERROR)
    result = reconcile_audits([candidate], batch, [decision])

    assert result.cases == ()
    assert result.dataset_revision_case_ids == (candidate.case_id,)
    assert result.insufficient_evidence_suites == (Suite.REWRITE,)
    assert result.pending_audit_ids == ()
    with pytest.raises(ValidationError):
        _decision(candidate, JudgeVerdict.PASS, disposition=AuditDisposition.GOLD_ERROR)


def test_required_audit_without_decision_is_not_silently_finalized():
    candidate = _candidate(1)
    batch = select_audit_batch([candidate], policy=AuditPolicy(seed=5, sample_rate=1.0))

    result = reconcile_audits([candidate], batch, [])

    assert result.cases == ()
    assert result.pending_audit_ids == (f"{candidate.case_id}:{candidate.subject}",)
