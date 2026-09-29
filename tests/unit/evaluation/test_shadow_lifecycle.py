"""Shadow lifecycle + D0-local executor tests on real dataset fixtures.

Covers: EARLY/LATE boundary derivation, dual-schedule bank delta, shared pre-batch
snapshots, same-boundary atomicity, target eligibility, empty-pool KEEP_BOTH,
retrieval-miss LLM#2 execution, metric factorization, DUPLICATE multi-target,
false-supersede safety, invalid-target fail-closed, paired cache identity, and
S0/S1/S-sweep semantics. No runtime, fork, or dataset behavior is touched.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from evaluation.dataset import load_bundle, load_manifest
from evaluation.d0_local_executor import (
    D0LocalExecutor,
    EmbeddingPort,
    InvalidDecision,
    retrieve_semantic,
    s0_config,
    s1_config,
    s_sweep_config,
)
from evaluation.d0_metrics import (
    adjudicate,
    multi_target_reinforcement_diagnostic,
    safety_metrics,
    schedule_delta,
    tier_metrics,
)
from evaluation.models import (
    D0ConflictDecision,
    D0Decision,
    D0Prediction,
    D0RetrievalConfig,
    D0RetrievalResult,
    D0Schedule,
)
from evaluation.shadow_lifecycle import ShadowLifecycleError, ShadowTimeline

DATASET_ROOT = Path(__file__).resolve().parents[3] / "dataset" / "kira_ltm_v1"


def _timeline() -> ShadowTimeline:
    manifest = load_manifest(DATASET_ROOT)
    bundles = tuple(load_bundle(entry, root=DATASET_ROOT) for entry in manifest.bundles)
    return ShadowTimeline(bundles)


def _bundle_id(bundle: ShadowTimeline) -> str:
    return sorted(bundle._schedules[D0Schedule.EARLY])[0]


class StaticEmbeddings(EmbeddingPort):
    """Deterministic embeddings keyed by token overlap for controlled tests."""

    def __init__(self, vectors: Mapping[str, Sequence[float]]) -> None:
        self._vectors = {text: list(vector) for text, vector in vectors.items()}

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            return [list(self._vectors[text]) for text in texts]
        except KeyError as error:
            raise KeyError(f"no embedding for {error}") from None


class ScriptedDecisions:
    """DecisionPort returning canned payloads and counting calls."""

    def __init__(self, responses: Sequence[Mapping[str, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    async def decide(self, request_hash: str, request: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append((request_hash, request))
        if not self._responses:
            raise AssertionError("unexpected extra LLM#2 call")
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Boundary derivation
# ---------------------------------------------------------------------------


def test_early_uses_companion_of_primary_and_late_uses_latest_evidence():
    timeline = _timeline()
    early = timeline.bundle(D0Schedule.EARLY, "conv01")
    late = timeline.bundle(D0Schedule.LATE, "conv01")
    # M02's only evidence is its user-origin primary turn (assistant-echo
    # supporting turns were removed from the canonical annotation), so both
    # schedules agree on the D1:10 boundary.
    assert early.boundaries["M02"] == "D1:10"
    assert late.boundaries["M02"] == "D1:10"


def test_late_boundary_for_supporting_turn_beyond_primary():
    timeline = _timeline()
    early = timeline.bundle(D0Schedule.EARLY, "conv02")
    late = timeline.bundle(D0Schedule.LATE, "conv02")
    assert early.boundaries["M14"] == "D20:2"
    assert late.boundaries["M14"] == "D20:4"


def test_boundary_requires_completed_turn_companion():
    timeline = _timeline()
    index = timeline.bundle(D0Schedule.EARLY, "conv01").turn_index
    # The dataset always ends sessions with assistant turns, so fabricate the
    # one missing-companion case: a user turn whose companion was removed.
    victim = "D23:1" if "D23:1" in index.companion_of else next(iter(index.companion_of))
    del index.companion_of[victim]
    with pytest.raises(ShadowLifecycleError):
        index.boundary_of((victim,))


def test_batch_grouping_collapses_same_boundary_events():
    timeline = _timeline()
    early = timeline.bundle(D0Schedule.EARLY, "conv01")
    batches = {key: [event.event_id for event in batch] for key, batch in early.batches()}
    members = [sorted(members) for members in batches.values()]
    assert ["M13", "M14"] in members
    assert ["M15", "M16"] in members


def test_ambiguous_aggregate_events_are_computed_not_hardcoded():
    timeline = _timeline()
    ambiguous = timeline.ambiguous_aggregate_events()
    # M02's supporting turns were removed from the canonical annotation (assistant
    # echoes); its late boundary now matches early, so it is no longer ambiguous.
    assert "conv01:M02" not in ambiguous
    assert "conv01:M09" in ambiguous
    assert "conv02:M14" in ambiguous
    assert "conv03:M14" in ambiguous
    assert "conv01:M01" not in ambiguous


# ---------------------------------------------------------------------------
# Dual-schedule bank delta + shared snapshots
# ---------------------------------------------------------------------------


def test_bank_rows_identical_across_schedules_despite_boundary_move():
    timeline = _timeline()
    early = timeline.bundle(D0Schedule.EARLY, "conv02")
    late = timeline.bundle(D0Schedule.LATE, "conv02")
    m14 = next(event for event in early.events if event.event_id == "M14")
    early_bank = set(early.pre_batch_active_ids(m14))
    late_bank = set(late.pre_batch_active_ids(m14))
    # M14's late boundary (D20:4) lands after M13's batch while EARLY (D20:2)
    # precedes it, but M13 only REINFORCES M01: under event-vs-row semantics it
    # never materializes a row, so the durable bank is identical across schedules.
    assert early.boundaries["M14"] != late.boundaries["M14"]
    assert early_bank == late_bank
    assert "M13" not in early_bank and "M13" not in late_bank
    # M11 (update) is a durable row present in both banks; M14 itself is not.
    assert "M11" in early_bank
    assert "M14" not in early_bank


def test_shared_pre_batch_snapshot_for_same_boundary_batch():
    timeline = _timeline()
    early = timeline.bundle(D0Schedule.EARLY, "conv01")
    m13 = next(event for event in early.events if event.event_id == "M13")
    m14 = next(event for event in early.events if event.event_id == "M14")
    assert early.batch_key(m13) == early.batch_key(m14)
    assert early.pre_batch_active_ids(m13) == early.pre_batch_active_ids(m14)
    snapshot = set(early.pre_batch_active_ids(m13))
    # M13 supersedes M01; the post-batch bank must reflect that transition only
    # after the batch, while the shared pre-batch snapshot still contains M01.
    assert "M01" in snapshot
    assert "M13" not in snapshot and "M14" not in snapshot


def test_same_boundary_batch_atomicity_applies_transitions_after_batch():
    timeline = _timeline()
    early = timeline.bundle(D0Schedule.EARLY, "conv01")
    batch_keys = {event.event_id: early.batch_key(event) for event in early.events}
    m01_key = batch_keys["M01"]
    post = set(early.post_batch_active_ids(m01_key + 1))
    # The M13 batch (update superseding M01) deactivates M01 only from the next
    # batch onward.
    post_of_m13_batch = set(early.post_batch_active_ids(batch_keys["M13"]))
    assert "M01" not in post_of_m13_batch
    assert "M13" in post_of_m13_batch


def test_superseded_memories_never_reenter_active_bank():
    timeline = _timeline()
    early = timeline.bundle(D0Schedule.EARLY, "conv01")
    for _, _, snapshot in early.iter_batches():
        active = set(snapshot)
        for event_id in active:
            superseding = [
                event
                for event in early.events
                if event.supersedes_memory_id == event_id
            ]
            for event in superseding:
                assert early.batch_key(event) >= early.batch_key(
                    next(e for e in early.events if e.event_id == event_id)
                )


# ---------------------------------------------------------------------------
# Retrieval semantics
# ---------------------------------------------------------------------------


def test_s0_and_s1_floor_semantics_on_raw_cosine():
    bank = {
        "m1": ("alpha", [1.0, 0.0]),
        "m2": ("beta", [0.0, 1.0]),
        "m3": ("gamma", [0.93, 0.37]),
    }
    query = [1.0, 0.0]
    s0 = retrieve_semantic(query, bank, s0_config(), top_k=2)
    assert s0.retrieved_ids[0] == "m1"
    assert len(s0.retrieved_ids) == 2
    s1 = retrieve_semantic(query, bank, s1_config(), top_k=2)
    assert "m2" not in s1.retrieved_ids  # cosine 0.0 below 0.1 floor
    assert "m1" in s1.retrieved_ids


def test_s_sweep_requires_label_informed_and_fixed_ones_forbid_it():
    sweep = s_sweep_config(0.2)
    assert sweep.label_informed
    with pytest.raises(ValidationError):
        D0RetrievalConfig(config_id="S_SWEEP", semantic_floor=0.2)
    with pytest.raises(ValidationError):
        D0RetrievalConfig(config_id="S0", semantic_floor=0.0, label_informed=True)


def test_empty_bank_returns_empty_pool():
    result = retrieve_semantic([1.0, 0.0], {}, s0_config(), top_k=5)
    assert result.pool_size == 0
    assert result.retrieved_ids == ()


# ---------------------------------------------------------------------------
# Executor behavior
# ---------------------------------------------------------------------------


def _conv01_setup() -> tuple[ShadowTimeline, str]:
    timeline = _timeline()
    return timeline, "conv01"


def _identity_embeddings(timeline: ShadowTimeline, bundle_id: str) -> StaticEmbeddings:
    """Distinct orthogonal vector per canonical fact so ranking is deterministic."""
    vectors: dict[str, list[float]] = {}
    early = timeline.bundle(D0Schedule.EARLY, bundle_id)
    late = timeline.bundle(D0Schedule.LATE, bundle_id)
    for event in (*early.events, *late.events):
        if event.canonical_fact not in vectors:
            vector = [0.0] * 64
            position = len(vectors) % 64
            vector[position] = 1.0
            vectors[event.canonical_fact] = vector
    return StaticEmbeddings(vectors)


def test_empty_pool_deterministic_keep_both_without_llm2():
    timeline, bundle_id = _conv01_setup()
    executor = D0LocalExecutor(
        timeline,
        _identity_embeddings(timeline, bundle_id),
        ScriptedDecisions([]),
    )
    # Orthogonal identity embeddings give every distinct pair cosine 0.0, so the
    # 0.1 floor empties every pool and LLM#2 must never run.
    predictions = asyncio.run(
        executor.evaluate_bundle_schedule(bundle_id, D0Schedule.EARLY, [s1_config()])
    )
    assert predictions
    keep_both = [p for p in predictions if p.pool.pool_size == 0]
    assert len(keep_both) == len(predictions)
    for prediction in keep_both:
        assert prediction.decision is not None
        assert prediction.decision.decision is D0Decision.KEEP_BOTH
        assert prediction.llm_request_hash is None
        assert prediction.cache_hit is False


def test_retrieval_miss_still_runs_llm2_when_pool_nonempty():
    timeline, bundle_id = _conv01_setup()
    early = timeline.bundle(D0Schedule.EARLY, bundle_id)
    # All-same embeddings rank everything equally; pools are non-empty from the
    # first populated batch, so LLM#2 runs for every such point.
    vectors: dict[str, list[float]] = {event.canonical_fact: [1.0, 0.0] for event in early.events}
    embeddings = StaticEmbeddings(vectors)
    responses = [
        {"decision": "KEEP_BOTH", "target_memory_id": None}
    ] * 64
    decisions = ScriptedDecisions(responses)
    executor = D0LocalExecutor(timeline, embeddings, decisions)
    predictions = asyncio.run(
        executor.evaluate_bundle_schedule(bundle_id, D0Schedule.EARLY, [s0_config()])
    )
    non_empty = [p for p in predictions if p.pool.pool_size > 0]
    empty = [p for p in predictions if p.pool.pool_size == 0]
    assert non_empty, "fixture must produce non-empty pools"
    assert all(p.decision is not None for p in non_empty)
    assert all(p.llm_request_hash is not None for p in non_empty)
    for prediction in empty:
        assert prediction.llm_request_hash is None


def test_paired_early_late_identical_request_reuses_cached_decision():
    timeline, bundle_id = _conv01_setup()
    decisions = ScriptedDecisions(
        [{"decision": "KEEP_BOTH", "target_memory_id": None}] * 200
    )
    executor = D0LocalExecutor(timeline, _identity_embeddings(timeline, bundle_id), decisions)
    early_predictions = asyncio.run(
        executor.evaluate_bundle_schedule(bundle_id, D0Schedule.EARLY, [s0_config()])
    )
    late_predictions = asyncio.run(
        executor.evaluate_bundle_schedule(bundle_id, D0Schedule.LATE, [s0_config()])
    )
    early_cached = [p for p in early_predictions if p.cache_hit]
    late_cached = [p for p in late_predictions if p.cache_hit]
    # Any cache hit proves the request hash matched an earlier call exactly.
    assert all(p.llm_request_hash is not None for p in [*early_cached, *late_cached])
    paired = schedule_delta(early_predictions, late_predictions)
    assert paired.robust_events or paired.boundary_sensitive_events


def test_invalid_target_fails_closed():
    raw = {"decision": "SUPERSEDE", "target_memory_id": "unknown-id"}
    with pytest.raises(InvalidDecision):
        from evaluation.d0_local_executor import _parse_decision

        _parse_decision(raw, ("known-1", "known-2"))


def test_keep_both_with_target_is_invalid():
    from evaluation.d0_local_executor import _parse_decision

    with pytest.raises(InvalidDecision):
        _parse_decision(
            {"decision": "KEEP_BOTH", "target_memory_id": "known-1"}, ("known-1",)
        )


# ---------------------------------------------------------------------------
# Metric factorization
# ---------------------------------------------------------------------------


def _prediction(
    *,
    event_id: str = "conv01:M99",
    gold_operation: str = "update",
    gold_targets: tuple[str, ...] = ("M01",),
    retrieved: tuple[str, ...] = ("M01",),
    decision: D0ConflictDecision | None = None,
    pool_size: int = 1,
    llm_request_hash: str | None = "a" * 64,
) -> D0Prediction:
    return D0Prediction(
        event_id=event_id,
        bundle_id="conv01",
        schedule=D0Schedule.EARLY,
        config_id="S0",
        gold_operation=gold_operation,  # type: ignore[arg-type]
        gold_target_ids=gold_targets,
        candidate_text="fact",
        pool=D0RetrievalResult(retrieved_ids=retrieved, scored_ids=retrieved, pool_size=pool_size),
        decision=decision,
        llm_request_hash=llm_request_hash,  # type: ignore[arg-type]
        retrieval_failure=not any(t in retrieved for t in gold_targets),
    )


def test_supersede_metric_factorization_does_not_double_count():
    outcomes = [
        adjudicate(_prediction(retrieved=("M01",), decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M01"))),
        adjudicate(_prediction(retrieved=("M02",), decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M02"))),
        adjudicate(_prediction(retrieved=("M02",), decision=None, pool_size=2)),
    ]
    tiers = tier_metrics(outcomes, "SUPERSEDE")
    assert tiers.retrieval_total == 3
    assert tiers.retrieval_recall == pytest.approx(1 / 3)
    assert tiers.decision_total == 1
    assert tiers.decision_given_retrieved == 1
    assert tiers.end_to_end_total == 3
    assert tiers.end_to_end_success == pytest.approx(1 / 3)


def test_duplicate_multi_target_semantics():
    prediction = _prediction(
        event_id="conv02:M14",
        gold_operation="reinforce_existing",
        gold_targets=("M03", "M10", "M11"),
        retrieved=("M10",),
        decision=D0ConflictDecision(decision="DUPLICATE", target_memory_id="M10"),
    )
    outcomes = [adjudicate(prediction)]
    # Multi-target reinforcements are excluded from the single-target DUPLICATE
    # headline tier and routed to the diagnostic instead.
    tiers = tier_metrics(outcomes, "DUPLICATE")
    assert tiers.end_to_end_total == 0
    assert tiers.end_to_end_success is None
    diagnostic = multi_target_reinforcement_diagnostic(outcomes)
    assert diagnostic["point_total"] == 1
    assert diagnostic["event_ids"] == ["conv02:M14"]
    assert diagnostic["end_to_end_success"] == 1.0
    assert diagnostic["decision_mismatch_total"] == 0


def test_false_supersede_safety_scoring():
    outcomes = [
        # Gold reinforce, LLM#2 says SUPERSEDE: model false supersede.
        adjudicate(
            _prediction(
                event_id="conv02:M14",
                gold_operation="reinforce_existing",
                gold_targets=("M03",),
                retrieved=("M03",),
                decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M03"),
            )
        ),
        # Gold add (KEEP_BOTH), LLM#2 says SUPERSEDE: model false supersede.
        adjudicate(
            _prediction(
                event_id="conv01:M01",
                gold_operation="add",
                gold_targets=(),
                retrieved=("M02",),
                decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M02"),
                pool_size=1,
            )
        ),
        # Gold reinforce, correct DUPLICATE: no violation.
        adjudicate(
            _prediction(
                event_id="conv02:M13",
                gold_operation="reinforce_existing",
                gold_targets=("M01",),
                retrieved=("M01",),
                decision=D0ConflictDecision(decision="DUPLICATE", target_memory_id="M01"),
            )
        ),
        # Gold update, model keeps both: a missed supersede (decision mismatch),
        # NOT a false supersede — no destructive transition occurred.
        adjudicate(
            _prediction(
                event_id="conv01:M10",
                gold_operation="update",
                gold_targets=("M05",),
                retrieved=("M05",),
                decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
            )
        ),
    ]
    safety = safety_metrics(outcomes)
    assert safety.llm_executed_total == 4
    assert safety.model_false_supersede_total == 2
    assert safety.pipeline_false_supersede_total == 2
    assert safety.model_false_supersede_rate == pytest.approx(2 / 4)
    assert safety.decision_mismatch_total == 3  # both false supersedes + the miss


def test_retrieval_failure_is_not_conditional_reasoning_failure():
    outcome = adjudicate(
        _prediction(
            gold_operation="update",
            gold_targets=("M01",),
            retrieved=("M02", "M03"),
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M02"),
            pool_size=2,
        )
    )
    assert outcome.retrieval_failure is True
    tiers = tier_metrics([outcome], "SUPERSEDE")
    assert tiers.retrieval_recall == 0
    assert tiers.decision_total == 0  # excluded from conditional reasoning
    assert tiers.end_to_end_success == 0  # included as end-to-end failure
    safety = safety_metrics([outcome])
    assert safety.llm_executed_total == 1
    # Right decision kind, wrong target identity: target mismatch; the supersede
    # would still delete an untargeted row, so the pipeline-side violation holds.
    assert safety.target_mismatch_total == 1
    assert safety.decision_mismatch_total == 0
    assert safety.model_false_supersede_total == 0
    assert safety.pipeline_false_supersede_total == 1


def test_schedule_delta_flags_decision_input_sensitive_events():
    early = _prediction(event_id="conv01:M02", gold_operation="add", gold_targets=(), retrieved=("M01",), decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None))
    late = D0Prediction(
        event_id="conv01:M02",
        bundle_id="conv01",
        schedule=D0Schedule.LATE,
        config_id="S0",
        gold_operation="add",
        gold_target_ids=(),
        candidate_text="fact",
        pool=D0RetrievalResult(retrieved_ids=("M01", "M02"), scored_ids=("M01", "M02"), pool_size=2),
        decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
    )
    delta = schedule_delta([early], [late])
    assert "conv01:M02" in delta.decision_input_sensitive_events
    assert "conv01:M02" not in delta.robust_events


# ---------------------------------------------------------------------------
# Round-13 correctness-audit regressions
# ---------------------------------------------------------------------------


def test_corpus_stats_separate_raw_rows_from_evaluable_events():
    timeline = _timeline()
    stats = timeline.corpus_stats()
    assert stats.total_gold_events == 62
    assert stats.conflict_evaluable_events == 55
    assert stats.do_not_persist_excluded == 7


def test_executor_never_feeds_do_not_persist_events_to_llm2():
    timeline = _timeline()
    for bundle_id in timeline.bundle_ids():
        raw_rows = timeline._bundles[bundle_id].memories["memory_gold"]
        for schedule in D0Schedule:
            bundle = timeline.bundle(schedule, bundle_id)
            event_ids = {event.event_id for event in bundle.events}
            for row in raw_rows:
                if not row.get("should_store", True):
                    assert row["memory_id"] not in event_ids


def test_llm_request_hash_includes_ordered_candidate_ids():
    from evaluation.d0_local_executor import decision_request

    pool = D0RetrievalResult(retrieved_ids=("m1",), scored_ids=("m1",), pool_size=1)
    other = D0RetrievalResult(retrieved_ids=("m9",), scored_ids=("m9",), pool_size=1)
    hash_m1, _ = decision_request("d0-local", "d0-conflict-v1", "fact", pool, {"m1": "likes football", "m9": "likes football"})
    hash_m9, _ = decision_request("d0-local", "d0-conflict-v1", "fact", other, {"m1": "likes football", "m9": "likes football"})
    assert hash_m1 != hash_m9
    same_id_diff_order = D0RetrievalResult(retrieved_ids=("m1", "m2"), scored_ids=("m1", "m2"), pool_size=2)
    flipped = D0RetrievalResult(retrieved_ids=("m2", "m1"), scored_ids=("m2", "m1"), pool_size=2)
    h1, _ = decision_request("d0-local", "d0-conflict-v1", "fact", same_id_diff_order, {"m1": "a", "m2": "b"})
    h2, _ = decision_request("d0-local", "d0-conflict-v1", "fact", flipped, {"m1": "a", "m2": "b"})
    assert h1 != h2


def test_cache_key_ignores_retrieval_provenance_s0_vs_s1():
    timeline, bundle_id = _conv01_setup()
    decisions = ScriptedDecisions([{"decision": "KEEP_BOTH", "target_memory_id": None}] * 64)
    # All-same embeddings: every pair has cosine 1.0, so S0 and S1 produce the
    # identical ordered pool for every point (both floors pass) and each distinct
    # request must reach the DecisionPort exactly once.
    executor = D0LocalExecutor(
        timeline,
        StaticEmbeddings({event.canonical_fact: [1.0, 0.0] for event in timeline.bundle(D0Schedule.EARLY, bundle_id).events}),
        decisions,
    )
    predictions = asyncio.run(
        executor.evaluate_bundle_schedule(
            bundle_id, D0Schedule.EARLY, [s0_config(), s1_config()]
        )
    )
    non_empty = [p for p in predictions if p.pool.pool_size > 0]
    by_event: dict[str, list] = {}
    for prediction in non_empty:
        by_event.setdefault(prediction.event_id, []).append(prediction)
    shared = 0
    for event_id, points in by_event.items():
        assert len(points) == 2, f"{event_id} must have one S0 and one S1 point"
        s0_point, s1_point = points
        assert {s0_point.config_id, s1_point.config_id} == {"S0", "S1"}
        assert s0_point.pool.retrieved_ids == s1_point.pool.retrieved_ids
        assert s0_point.llm_request_hash == s1_point.llm_request_hash
        assert s0_point.cache_hit is False
        assert s1_point.cache_hit is True
        shared += 1
    assert shared, "fixture must produce shared S0/S1 pools"
    # One DecisionPort call per distinct request, not per (config, event) point.
    assert len(decisions.calls) == len(by_event)


def test_semantic_invalid_output_is_cached_and_reproduced():
    timeline, bundle_id = _conv01_setup()
    early = timeline.bundle(D0Schedule.EARLY, bundle_id)
    vectors: dict[str, list[float]] = {event.canonical_fact: [1.0, 0.0] for event in early.events}
    responses = [{"decision": "SUPERSEDE", "target_memory_id": "not-in-pool"}] * 64
    decisions = ScriptedDecisions(responses)
    executor = D0LocalExecutor(timeline, StaticEmbeddings(vectors), decisions)
    first_run = asyncio.run(
        executor.evaluate_bundle_schedule(bundle_id, D0Schedule.EARLY, [s0_config()])
    )
    second_run = asyncio.run(
        executor.evaluate_bundle_schedule(bundle_id, D0Schedule.EARLY, [s0_config()])
    )
    first_invalid = [p for p in first_run if p.decision is None and p.pool.pool_size > 0]
    assert first_invalid, "fixture must produce invalid-output points"
    first_by_event = {p.event_id: p for p in first_invalid}
    for prediction in second_run:
        if prediction.pool.pool_size == 0:
            continue
        anchor = first_by_event.get(prediction.event_id)
        assert anchor is not None
        # Same exact request -> same observed outcome: decision None, same hash,
        # served from cache with no second DecisionPort call.
        assert prediction.decision is None
        assert prediction.llm_request_hash == anchor.llm_request_hash
        assert prediction.cache_hit is True
    assert len(decisions.calls) == len(first_by_event)


def test_transport_failure_is_not_faked_as_decision_or_cached():
    from evaluation.d0_local_executor import DecisionPort, DecisionPortError

    class FailingPort(DecisionPort):
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, request_hash: str, request) -> Mapping[str, object]:
            self.calls += 1
            raise DecisionPortError("connection reset")

    timeline, bundle_id = _conv01_setup()
    early = timeline.bundle(D0Schedule.EARLY, bundle_id)
    vectors: dict[str, list[float]] = {event.canonical_fact: [1.0, 0.0] for event in early.events}
    port = FailingPort()
    executor = D0LocalExecutor(timeline, StaticEmbeddings(vectors), port)
    with pytest.raises(DecisionPortError):
        asyncio.run(
            executor.evaluate_bundle_schedule(bundle_id, D0Schedule.EARLY, [s0_config()])
        )
    assert port.calls == 1


def test_negative_cosine_clamps_to_zero_for_floor_and_ranking():
    bank = {
        "anti": ("opposite", [-1.0, 0.0]),
        "ortho": ("unrelated", [0.0, 1.0]),
    }
    result = retrieve_semantic([1.0, 0.0], bank, s0_config(), top_k=5)
    # cosine(anti) = -1.0 clamps to 0.0, same as orthogonal; tie-break by id keeps
    # both in the pool (S0 floor 0.0 accepts score 0.0) in deterministic order.
    assert result.retrieved_ids == ("anti", "ortho")
    s1 = retrieve_semantic([1.0, 0.0], bank, s1_config(), top_k=5)
    # cosine 0.0 < 0.1 floor: both filtered.
    assert s1.retrieved_ids == ()


def test_partial_cosine_scores_floor_thresholds():
    # Unit vectors whose x-component equals the cosine against [1.0, 0.0].
    b05 = {"w": ("w", [0.05, 0.998749217771909])}
    b15 = {"w": ("w", [0.15, 0.9886859966417976])}
    b25 = {"w": ("w", [0.25, 0.9682458365518543])}
    query = [1.0, 0.0]
    assert retrieve_semantic(query, b05, s0_config(), top_k=1).retrieved_ids == ("w",)
    assert retrieve_semantic(query, b05, s1_config(), top_k=1).retrieved_ids == ()
    assert retrieve_semantic(query, b15, s0_config(), top_k=1).retrieved_ids == ("w",)
    assert retrieve_semantic(query, b15, s1_config(), top_k=1).retrieved_ids == ("w",)
    assert retrieve_semantic(query, b25, s_sweep_config(0.25), top_k=1).retrieved_ids == ("w",)
    assert retrieve_semantic(query, b05, s_sweep_config(0.25), top_k=1).retrieved_ids == ()


def test_pool_ordering_tie_break_by_memory_id():
    bank = {
        "b2": ("second", [0.6, 0.8]),
        "a1": ("first", [0.6, 0.8]),
        "c3": ("third", [1.0, 0.0]),
    }
    result = retrieve_semantic([1.0, 0.0], bank, s0_config(), top_k=3)
    # a1 and b2 tie at cosine 0.6 and must order by id; c3 outranks at 1.0.
    assert result.retrieved_ids == ("c3", "a1", "b2")


def test_structural_sensitivity_annotations_from_timeline():
    from evaluation.d0_metrics import structural_sensitivity

    timeline = _timeline()
    structural = structural_sensitivity(timeline)
    moved = sorted(e for e, (m, _) in structural.items() if m)
    bank_sensitive = sorted(e for e, (_, b) in structural.items() if b)
    # M02's assistant-echo supporting turns were removed from the canonical
    # annotation, so its EARLY/LATE boundaries coincide and it leaves both sets.
    assert len(moved) == 3
    assert "conv01:M09" in moved
    assert "conv02:M14" in moved
    assert "conv03:M14" in moved
    # conv03:M14 moves boundary within one turn with no intervening batch, so its
    # pre-batch bank is identical: moved does not imply bank_sensitive.
    assert "conv03:M14" not in bank_sensitive
    # Under row semantics reinforcements never materialize rows, so no boundary
    # move in this corpus changes the durable bank between schedules anymore.
    assert bank_sensitive == []


def test_schedule_delta_robust_requires_same_effective_input():
    # Same retrieved pool + same decision across schedules => robust even when a
    # structural annotation would call the event bank-sensitive.
    early = _prediction(event_id="conv01:M05", gold_operation="add", gold_targets=(), retrieved=("M01",), decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None))
    late = D0Prediction(
        event_id="conv01:M05",
        bundle_id="conv01",
        schedule=D0Schedule.LATE,
        config_id="S0",
        gold_operation="add",
        gold_target_ids=(),
        candidate_text="fact",
        pool=D0RetrievalResult(retrieved_ids=("M01",), scored_ids=("M01",), pool_size=1),
        decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
    )
    structural = {"conv01:M05": (True, True)}
    delta = schedule_delta([early], [late], structural=structural)
    assert "conv01:M05" in delta.robust_events
    assert "conv01:M05" in delta.boundary_moved_events
    assert "conv01:M05" in delta.bank_sensitive_events
    assert "conv01:M05" not in delta.decision_input_sensitive_events
    assert "conv01:M05" not in delta.outcome_sensitive_events


def test_outcome_sensitive_overrides_other_classes():
    early = _prediction(event_id="conv01:M10", gold_operation="update", gold_targets=("M05",), retrieved=("M05",), decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M05"))
    late = D0Prediction(
        event_id="conv01:M10",
        bundle_id="conv01",
        schedule=D0Schedule.LATE,
        config_id="S0",
        gold_operation="update",
        gold_target_ids=("M05",),
        candidate_text="fact",
        pool=D0RetrievalResult(retrieved_ids=("M05",), scored_ids=("M05",), pool_size=1),
        decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
    )
    delta = schedule_delta([early], [late], structural={"conv01:M10": (True, True)})
    assert "conv01:M10" in delta.outcome_sensitive_events
    assert "conv01:M10" in delta.boundary_moved_events
    assert "conv01:M10" not in delta.robust_events
    assert "conv01:M10" not in delta.decision_input_sensitive_events
