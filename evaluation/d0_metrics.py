"""Factored metrics for the D0-local O/O conflict-stage characterization.

Three tiers never double-count: retrieval recall denominators include every gold
point, conditional decision metrics exclude points whose gold target was not
retrieved, and end-to-end metrics include retrieval failures as failures. Safety
scoring runs on every LLM#2 output regardless of retrieval success.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evaluation.models import D0ConflictDecision, D0Decision, D0Prediction, D0Schedule
from evaluation.shadow_lifecycle import ShadowTimeline


@dataclass(frozen=True, slots=True)
class PointOutcome:
    """Per-point adjudication; gold drives scoring, never execution."""

    event_id: str
    schedule: D0Schedule
    config_id: str
    gold_operation: str
    gold_target_ids: tuple[str, ...]
    retrieved_ids: tuple[str, ...]
    pool_empty: bool
    retrieval_failure: bool
    decision_valid: bool
    decision: D0ConflictDecision | None
    # Safety flags, computed for every executed LLM#2 call.
    model_false_supersede: bool | None
    wrong_target: bool | None
    # End-to-end adjudication.
    end_to_end_success: bool | None


def _allowed_duplicate_targets(prediction: D0Prediction) -> frozenset[str]:
    return frozenset(prediction.gold_target_ids)


def adjudicate(prediction: D0Prediction) -> PointOutcome:
    """Adjudicate one prediction. Gold target presence gates conditional metrics
    only; execution already happened (deterministic KEEP_BOTH or LLM#2).

    Gold target identity: update events carry exactly one supersedes target
    (first element of gold_target_ids by construction); reinforce events carry
    one-or-more allowed duplicate targets."""
    if prediction.gold_operation == "update" and prediction.gold_target_ids:
        gold_kind = "SUPERSEDE"
        gold_target = prediction.gold_target_ids[0]
    elif prediction.gold_operation == "reinforce_existing":
        gold_kind = "DUPLICATE"
        gold_target = None
    elif prediction.gold_operation == "add":
        gold_kind = "KEEP_BOTH"
        gold_target = None
    else:
        gold_kind = "NEGATIVE"
        gold_target = None

    decision = prediction.decision
    decision_valid = decision is not None
    pool_empty = prediction.pool.pool_size == 0

    model_false_supersede: bool | None = None
    wrong_target: bool | None = None
    end_to_end: bool | None = None

    if gold_kind == "SUPERSEDE":
        if decision is not None:
            model_false_supersede = decision.decision is not D0Decision.SUPERSEDE
            if decision.decision is D0Decision.SUPERSEDE:
                wrong_target = decision.target_memory_id != gold_target
            else:
                wrong_target = True
        end_to_end = (
            decision is not None
            and decision.decision is D0Decision.SUPERSEDE
            and decision.target_memory_id == gold_target
        )
    elif gold_kind == "DUPLICATE":
        if decision is not None:
            model_false_supersede = decision.decision is D0Decision.SUPERSEDE
            if decision.decision is D0Decision.DUPLICATE:
                wrong_target = decision.target_memory_id not in _allowed_duplicate_targets(
                    prediction
                )
            else:
                wrong_target = True
        end_to_end = (
            decision is not None
            and decision.decision is D0Decision.DUPLICATE
            and decision.target_memory_id in _allowed_duplicate_targets(prediction)
        )
    else:
        if decision is not None:
            model_false_supersede = decision.decision is D0Decision.SUPERSEDE
            if decision.decision is D0Decision.SUPERSEDE:
                wrong_target = True
        end_to_end = decision is None or decision.decision is not D0Decision.SUPERSEDE

    return PointOutcome(
        event_id=prediction.event_id,
        schedule=prediction.schedule,
        config_id=prediction.config_id,
        gold_operation=prediction.gold_operation,
        gold_target_ids=prediction.gold_target_ids,
        retrieved_ids=prediction.pool.retrieved_ids,
        pool_empty=pool_empty,
        retrieval_failure=prediction.retrieval_failure,
        decision_valid=decision_valid,
        decision=decision,
        model_false_supersede=model_false_supersede,
        wrong_target=wrong_target,
        end_to_end_success=end_to_end,
    )


@dataclass(frozen=True, slots=True)
class TierMetrics:
    retrieval_recall: float | None
    decision_given_retrieved: float | None
    end_to_end_success: float | None
    retrieval_total: int
    decision_total: int
    end_to_end_total: int


@dataclass(frozen=True, slots=True)
class SafetyMetrics:
    model_false_supersede_rate: float
    model_false_supersede_total: int
    pipeline_false_supersede_rate: float
    pipeline_false_supersede_total: int
    wrong_target_rate: float
    wrong_target_total: int
    invalid_output_rate: float
    invalid_output_total: int


@dataclass(frozen=True, slots=True)
class ScheduleDelta:
    """Five-class schedule sensitivity per event; most-severe class wins.

    Classes are cumulative in specificity: boundary_moved (schedule assigns a
    different formation boundary) implies nothing about bank/pool yet; bank_sensitive
    (pre-batch ACTIVE bank differs) may or may not change retrieval;
    retrieval_sensitive (retrieved pool differs) may or may not change the decision
    input; decision_input_sensitive (the ordered LLM#2 candidate payload differs)
    may or may not change the outcome. outcome_sensitive means the observed decision
    differs across schedules. robust means none of the above applies. Only robust
    events support invariant-across-schedules conclusions."""

    robust_events: tuple[str, ...] = ()
    boundary_moved_events: tuple[str, ...] = ()
    bank_sensitive_events: tuple[str, ...] = ()
    retrieval_sensitive_events: tuple[str, ...] = ()
    decision_input_sensitive_events: tuple[str, ...] = ()
    outcome_sensitive_events: tuple[str, ...] = ()


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def tier_metrics(outcomes: Sequence[PointOutcome], kind: str) -> TierMetrics:
    """kind: SUPERSEDE | DUPLICATE | KEEP_BOTH."""
    if kind == "KEEP_BOTH":
        relevant = [o for o in outcomes if o.gold_operation == "add"]
        correct = [o for o in relevant if o.end_to_end_success]
        return TierMetrics(
            retrieval_recall=None,
            decision_given_retrieved=None,
            end_to_end_success=_ratio(len(correct), len(relevant)),
            retrieval_total=0,
            decision_total=0,
            end_to_end_total=len(relevant),
        )
    if kind == "SUPERSEDE":
        relevant = [o for o in outcomes if o.gold_operation == "update" and o.gold_target_ids]
        retrieved = [o for o in relevant if o.gold_target_ids[0] in o.retrieved_ids]
        decided_correct = [
            o
            for o in retrieved
            if o.decision is not None
            and o.decision.decision is D0Decision.SUPERSEDE
            and o.decision.target_memory_id == o.gold_target_ids[0]
        ]
        end_to_end_correct = [o for o in relevant if o.end_to_end_success]
        return TierMetrics(
            retrieval_recall=_ratio(len(retrieved), len(relevant)),
            decision_given_retrieved=_ratio(len(decided_correct), len(retrieved)),
            end_to_end_success=_ratio(len(end_to_end_correct), len(relevant)),
            retrieval_total=len(relevant),
            decision_total=len(retrieved),
            end_to_end_total=len(relevant),
        )
    # DUPLICATE
    relevant = [o for o in outcomes if o.gold_operation == "reinforce_existing"]
    allowed = {o.event_id: o.gold_target_ids for o in relevant}
    retrieved = [o for o in relevant if allowed[o.event_id] and any(t in o.retrieved_ids for t in allowed[o.event_id])]
    decided_correct = [
        o
        for o in retrieved
        if o.decision is not None
        and o.decision.decision is D0Decision.DUPLICATE
        and o.decision.target_memory_id in allowed[o.event_id]
    ]
    end_to_end_correct = [o for o in relevant if o.end_to_end_success]
    return TierMetrics(
        retrieval_recall=_ratio(len(retrieved), len(relevant)),
        decision_given_retrieved=_ratio(len(decided_correct), len(retrieved)),
        end_to_end_success=_ratio(len(end_to_end_correct), len(relevant)),
        retrieval_total=len(relevant),
        decision_total=len(retrieved),
        end_to_end_total=len(relevant),
    )


def safety_metrics(outcomes: Sequence[PointOutcome]) -> SafetyMetrics:
    """Safety scoring over every executed LLM#2 call; retrieval failures included.

    pipeline_false_supersede additionally counts supersede transitions that would
    enter state without gold justification (wrong-target SUPERSEDE on gold SUPERSEDE
    points is a pipeline false supersede too)."""
    executed = [o for o in outcomes if o.decision is not None]
    model_false = [o for o in executed if o.model_false_supersede]
    wrong_target = [o for o in executed if o.wrong_target]
    invalid = [
        o for o in outcomes if o.decision is None and not o.pool_empty
    ]
    pipeline_false = [
        o
        for o in executed
        if o.decision is not None
        and o.decision.decision is D0Decision.SUPERSEDE
        and (
            o.gold_operation != "update"
            or not o.gold_target_ids
            or o.decision.target_memory_id != o.gold_target_ids[0]
        )
    ]
    return SafetyMetrics(
        model_false_supersede_rate=_ratio(len(model_false), len(executed)) or 0.0,
        model_false_supersede_total=len(model_false),
        pipeline_false_supersede_rate=_ratio(len(pipeline_false), len(executed)) or 0.0,
        pipeline_false_supersede_total=len(pipeline_false),
        wrong_target_rate=_ratio(len(wrong_target), len(executed)) or 0.0,
        wrong_target_total=len(wrong_target),
        invalid_output_rate=_ratio(len(invalid), len(outcomes)) or 0.0,
        invalid_output_total=len(invalid),
    )


def structural_sensitivity(timeline: ShadowTimeline) -> dict[str, tuple[bool, bool]]:
    """Per gold event: (boundary_moved, bank_sensitive) from annotations only.

    boundary_moved: EARLY and LATE schedules assign different formation boundaries.
    bank_sensitive: the pre-batch ACTIVE bank the candidate actually sees differs
    between schedules. Both are structural facts, independent of any LLM output."""
    structural: dict[str, tuple[bool, bool]] = {}
    for bundle_id, event, _early_b, _late_b in timeline.boundary_pairs():
        early_bundle = timeline.bundle(D0Schedule.EARLY, bundle_id)
        late_bundle = timeline.bundle(D0Schedule.LATE, bundle_id)
        moved = early_bundle.boundaries[event.event_id] != late_bundle.boundaries[event.event_id]
        bank_diff = (
            early_bundle.pre_batch_active_ids(event) != late_bundle.pre_batch_active_ids(event)
        )
        structural[f"{bundle_id}:{event.event_id}"] = (moved, bank_diff)
    return structural


def schedule_delta(
    predictions_early: Sequence[D0Prediction],
    predictions_late: Sequence[D0Prediction],
    *,
    structural: Mapping[str, tuple[bool, bool]] | None = None,
) -> ScheduleDelta:
    """Pair EARLY/LATE predictions by (event, config); identical LLM requests reuse
    the cached decision, so differences trace to bank/pool composition, not noise.

    The robust set is defined by the *effective* retrieval/decision input, never by
    the boundary timestamp: an event is robust when every config yields the same
    ordered retrieved pool and the same decision across schedules, even if the
    boundary moved or the bank differs elsewhere. decision_input_sensitive and
    outcome_sensitive are mutually exclusive with robust; boundary_moved,
    bank_sensitive and retrieval_sensitive are diagnostic annotations that may
    overlap with robust (structural or sub-cut movement with unchanged effective
    decision input)."""
    early_by_event = {(p.event_id, p.config_id): p for p in predictions_early}
    late_by_event = {(p.event_id, p.config_id): p for p in predictions_late}
    effective: dict[str, str] = {}
    scored_moved: set[str] = set()
    for key, early in early_by_event.items():
        late = late_by_event.get(key)
        if late is None:
            continue
        event_key = key[0]
        if early.decision is None and late.decision is None:
            decision_same = True
        elif early.decision is None or late.decision is None:
            decision_same = False
        else:
            decision_same = (
                early.decision.decision is late.decision.decision
                and early.decision.target_memory_id == late.decision.target_memory_id
            )
        input_same = (
            early.pool.retrieved_ids == late.pool.retrieved_ids
            and early.candidate_text == late.candidate_text
        )
        if not decision_same:
            effective[event_key] = "outcome_sensitive"
        elif not input_same:
            effective.setdefault(event_key, "decision_input_sensitive")
        else:
            effective.setdefault(event_key, "robust")
            if early.pool.scored_ids != late.pool.scored_ids:
                scored_moved.add(event_key)
    if structural is not None:
        boundary_moved = tuple(sorted(e for e, (m, _) in structural.items() if m))
        bank_sensitive = tuple(sorted(e for e, (_, b) in structural.items() if b))
    else:
        boundary_moved = ()
        bank_sensitive = ()
    return ScheduleDelta(
        robust_events=tuple(sorted(k for k, v in effective.items() if v == "robust")),
        boundary_moved_events=boundary_moved,
        bank_sensitive_events=bank_sensitive,
        retrieval_sensitive_events=tuple(sorted(scored_moved)),
        decision_input_sensitive_events=tuple(
            sorted(k for k, v in effective.items() if v == "decision_input_sensitive")
        ),
        outcome_sensitive_events=tuple(
            sorted(k for k, v in effective.items() if v == "outcome_sensitive")
        ),
    )


def dataset_limitations(
    predictions: Sequence[D0Prediction],
    gold_review_status: str,
) -> dict[str, object]:
    """Mandatory dataset-defect and scope-limitation disclosure for every report.

    These are characterization boundaries of the current corpus, not findings;
    none of them is fixed inside D0-local."""
    return {
        "exact_formation_boundary": "missing_from_dataset",
        "schedule_semantics": "EARLY/LATE are sensitivity bounds, not gold truth",
        "boundary_sensitive_event_count": len(
            {p.event_id for p in predictions if p.ambiguous_aggregate}
        ),
        "evidence_clipped_event_count": len(
            {p.event_id for p in predictions if p.evidence_clipped}
        ),
        "memory_scope": "absent from gold annotations; CONVERSATION-only local scope",
        "gold_review_status": gold_review_status,
        "holdout_corpus": "none; full-corpus results are not unseen-holdout evidence",
        "global_scope_cases": "missing from dataset; not measurable in D0-local",
        "cross_scope_cases": "missing from dataset; not measurable in D0-local",
        "kpi_different_period_slice": "missing from dataset",
        "exact_hash_positive_cases": "missing from dataset",
        "retrieval_mode": "semantic-only; production lexical search is PostgreSQL FTS "
        "ts_rank_cd, not reproduced offline (H1 DB-backed hybrid is future work)",
    }
