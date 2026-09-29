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
    """Per-point adjudication; gold drives scoring, never execution.

    ``model_false_supersede``/``pipeline_false_supersede`` are only meaningful on
    points where LLM#2 actually executed (``llm_executed``); deterministic
    pool-empty fallbacks carry None for every safety flag. ``decision_mismatch``
    and ``target_mismatch`` are orthogonal: a wrong decision kind is a decision
    mismatch; a wrong target identity given the right kind is a target mismatch."""

    event_id: str
    schedule: D0Schedule
    config_id: str
    gold_operation: str
    gold_target_ids: tuple[str, ...]
    retrieved_ids: tuple[str, ...]
    pool_empty: bool
    retrieval_failure: bool
    # True only when LLM#2 actually ran (request hash present); deterministic
    # pool-empty fallbacks are excluded from execution denominators.
    llm_executed: bool
    decision_valid: bool
    decision: D0ConflictDecision | None
    # Safety flags, computed for every executed LLM#2 call.
    model_false_supersede: bool | None
    decision_mismatch: bool | None
    target_mismatch: bool | None
    # End-to-end adjudication.
    end_to_end_success: bool | None


def _gold_kind(prediction: D0Prediction) -> str:
    """Single-target headline tier of a gold point.

    Multi-target reinforcements (len(gold_target_ids) > 1) cannot represent the
    one-target DUPLICATE contract cleanly; callers route them to the
    multi-target diagnostic instead of the headline tier."""
    if prediction.gold_operation == "update" and prediction.gold_target_ids:
        return "SUPERSEDE"
    if prediction.gold_operation == "reinforce_existing":
        return "DUPLICATE"
    if prediction.gold_operation == "add":
        return "KEEP_BOTH"
    return "NEGATIVE"


def is_multi_target_reinforcement(prediction: D0Prediction) -> bool:
    """Generic condition: reinforcement confirming more than one existing memory
    (the LLM#2 contract carries exactly one target per decision)."""
    return (
        prediction.gold_operation == "reinforce_existing"
        and len(prediction.gold_target_ids) > 1
    )


def _allowed_duplicate_targets(prediction: D0Prediction) -> frozenset[str]:
    return frozenset(prediction.gold_target_ids)


def adjudicate(prediction: D0Prediction) -> PointOutcome:
    """Adjudicate one prediction. Gold target presence gates conditional metrics
    only; execution already happened (deterministic KEEP_BOTH or LLM#2).

    Semantics:
    - ``model_false_supersede``: the MODEL actually returned SUPERSEDE while the
      gold operation does not justify any destructive supersede (gold add or
      reinforce). A missed supersede (model keeps both on a gold update) is NOT
      a false supersede — it is a decision mismatch.
    - ``decision_mismatch``: the returned decision kind is wrong for the gold
      operation (applies when a decision exists).
    - ``target_mismatch``: the decision kind was right but the chosen target is
      not the gold target (SUPERSEDE) / outside the allowed set (DUPLICATE).
    - End-to-end requires the gold target to actually be retrieved (fail-closed:
      a correct target ID out of pool is not an observable success)."""
    gold_kind = _gold_kind(prediction)
    gold_target = prediction.gold_target_ids[0] if prediction.gold_target_ids else None

    decision = prediction.decision
    decision_valid = decision is not None
    pool_empty = prediction.pool.pool_size == 0
    llm_executed = prediction.llm_request_hash is not None

    model_false_supersede: bool | None = None
    decision_mismatch: bool | None = None
    target_mismatch: bool | None = None
    end_to_end: bool | None = None

    def _retrieved(target: str | None) -> bool:
        return target is not None and target in prediction.pool.retrieved_ids

    if llm_executed and decision is not None:
        model_false_supersede = (
            decision.decision is D0Decision.SUPERSEDE
            and prediction.gold_operation != "update"
        )
        if gold_kind == "SUPERSEDE":
            decision_mismatch = decision.decision is not D0Decision.SUPERSEDE
            target_mismatch = (
                decision.decision is D0Decision.SUPERSEDE
                and decision.target_memory_id != gold_target
            )
        elif gold_kind == "DUPLICATE":
            decision_mismatch = decision.decision is not D0Decision.DUPLICATE
            target_mismatch = (
                decision.decision is D0Decision.DUPLICATE
                and decision.target_memory_id not in _allowed_duplicate_targets(prediction)
            )
        else:  # KEEP_BOTH gold
            decision_mismatch = decision.decision is not D0Decision.KEEP_BOTH
            # A target-bearing decision on a target-less gold is a target error.
            target_mismatch = decision.target_memory_id is not None

    if gold_kind == "SUPERSEDE":
        end_to_end = (
            decision is not None
            and decision.decision is D0Decision.SUPERSEDE
            and decision.target_memory_id == gold_target
            and _retrieved(gold_target)
        )
    elif gold_kind == "DUPLICATE":
        end_to_end = (
            decision is not None
            and decision.decision is D0Decision.DUPLICATE
            and decision.target_memory_id in _allowed_duplicate_targets(prediction)
            and _retrieved(decision.target_memory_id)
        )
    else:  # KEEP_BOTH gold (add): only an exact, valid KEEP_BOTH is success.
        end_to_end = (
            decision is not None
            and decision.decision is D0Decision.KEEP_BOTH
            and decision.target_memory_id is None
        )

    return PointOutcome(
        event_id=prediction.event_id,
        schedule=prediction.schedule,
        config_id=prediction.config_id,
        gold_operation=prediction.gold_operation,
        gold_target_ids=prediction.gold_target_ids,
        retrieved_ids=prediction.pool.retrieved_ids,
        pool_empty=pool_empty,
        retrieval_failure=prediction.retrieval_failure,
        llm_executed=llm_executed,
        decision_valid=decision_valid,
        decision=decision,
        model_false_supersede=model_false_supersede,
        decision_mismatch=decision_mismatch,
        target_mismatch=target_mismatch,
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
    """Safety counters. ``llm_executed_total`` denominates every flag: only
    points where LLM#2 actually executed (request hash present) enter the
    denominator; deterministic pool-empty fallbacks never called LLM#2 and are
    excluded. ``decision_mismatch_total``/``target_mismatch_total`` replace the
    former conflated ``wrong_target`` counter (kept below as a legacy union)."""

    llm_executed_total: int
    model_false_supersede_rate: float
    model_false_supersede_total: int
    pipeline_false_supersede_rate: float
    pipeline_false_supersede_total: int
    decision_mismatch_rate: float
    decision_mismatch_total: int
    target_mismatch_rate: float
    target_mismatch_total: int
    invalid_output_rate: float
    invalid_output_total: int


@dataclass(frozen=True, slots=True)
class ScheduleDelta:
    """Schedule sensitivity per event, in two groups with different semantics.

    Mutually exclusive effective classes (exactly one per event, worst wins):
    ``outcome_sensitive`` (observed decision differs across schedules) >
    ``decision_input_sensitive`` (ordered LLM#2 candidate payload differs) >
    ``robust`` (identical decision AND decision input everywhere). These three
    partition every evaluated event.

    Diagnostic annotations that OVERLAP with the classes above and never
    redefine them: ``boundary_moved`` (schedules assign different formation
    boundaries; structural, from annotations only), ``bank_sensitive``
    (pre-batch ACTIVE row bank differs; structural), ``scored_order_sensitive``
    (retrieved pool identical but the scored ordering beyond top-k moved).
    Only ``robust`` events support invariant-across-schedules conclusions;
    boundary/bank movement with an unchanged effective decision input is still
    robust."""

    robust_events: tuple[str, ...] = ()
    boundary_moved_events: tuple[str, ...] = ()
    bank_sensitive_events: tuple[str, ...] = ()
    scored_order_sensitive_events: tuple[str, ...] = ()
    decision_input_sensitive_events: tuple[str, ...] = ()
    outcome_sensitive_events: tuple[str, ...] = ()


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def tier_metrics(outcomes: Sequence[PointOutcome], kind: str) -> TierMetrics:
    """kind: SUPERSEDE | DUPLICATE | KEEP_BOTH. Multi-target reinforcements are
    excluded from the DUPLICATE headline tier (single-target contract); route
    them to :func:`multi_target_reinforcement_metrics` for diagnostics."""
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
    # DUPLICATE — single-target reinforcements only.
    relevant = [
        o
        for o in outcomes
        if o.gold_operation == "reinforce_existing" and len(o.gold_target_ids) == 1
    ]
    allowed = {o.event_id: o.gold_target_ids for o in relevant}
    retrieved = [
        o
        for o in relevant
        if allowed[o.event_id] and any(t in o.retrieved_ids for t in allowed[o.event_id])
    ]
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


def multi_target_reinforcement_diagnostic(outcomes: Sequence[PointOutcome]) -> dict[str, object]:
    """Diagnostic summary for multi-target reinforcement points: excluded from
    the single-target DUPLICATE headline tier, fully retained here for audit."""
    relevant = [
        o
        for o in outcomes
        if o.gold_operation == "reinforce_existing" and len(o.gold_target_ids) > 1
    ]
    correct = [o for o in relevant if o.end_to_end_success]
    return {
        "point_total": len(relevant),
        "event_ids": sorted({o.event_id for o in relevant}),
        "end_to_end_success": _ratio(len(correct), len(relevant)),
        "decision_mismatch_total": sum(1 for o in relevant if o.decision_mismatch),
    }


def safety_metrics(outcomes: Sequence[PointOutcome]) -> SafetyMetrics:
    """Safety scoring over every ACTUAL LLM#2 execution; deterministic pool-empty
    fallbacks (no request hash) never called the model and are excluded from the
    denominator. Invalid outputs (executed but no decision) are counted as
    invalid; they cannot count as any decision-kind safety flag.

    pipeline_false_supersede additionally counts destructive supersede
    transitions that would enter state without full gold justification: a
    SUPERSEDE on gold add/reinforce, or a wrong-target SUPERSEDE on a gold
    update (the target row would be deleted without gold basis)."""
    executed = [o for o in outcomes if o.llm_executed]
    model_false = [o for o in executed if o.model_false_supersede]
    decision_mismatch = [o for o in executed if o.decision_mismatch]
    target_mismatch = [o for o in executed if o.target_mismatch]
    invalid = [o for o in outcomes if o.llm_executed and o.decision is None]
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
        llm_executed_total=len(executed),
        model_false_supersede_rate=_ratio(len(model_false), len(executed)) or 0.0,
        model_false_supersede_total=len(model_false),
        pipeline_false_supersede_rate=_ratio(len(pipeline_false), len(executed)) or 0.0,
        pipeline_false_supersede_total=len(pipeline_false),
        decision_mismatch_rate=_ratio(len(decision_mismatch), len(executed)) or 0.0,
        decision_mismatch_total=len(decision_mismatch),
        target_mismatch_rate=_ratio(len(target_mismatch), len(executed)) or 0.0,
        target_mismatch_total=len(target_mismatch),
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
    boundary moved or the bank differs elsewhere. The effective classes are
    mutually exclusive and exhaustive; boundary_moved/bank_sensitive/
    scored_order_sensitive are overlapping diagnostic annotations (see
    ScheduleDelta)."""
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
        scored_order_sensitive_events=tuple(sorted(scored_moved)),
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
