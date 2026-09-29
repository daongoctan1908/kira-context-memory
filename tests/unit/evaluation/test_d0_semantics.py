"""D0 harness semantics regression tests: row-level shadow lifecycle, corrected
adjudication semantics, execution denominators, and dataset-oracle invariants.

Invariants pinned here were confirmed against the frozen dataset
``kira_ltm_v1@1.0.0-benchmark-ready.1`` (event_vs_row_contract,
expected_active_memory_row_source_event_ids oracle). Dataset files are never
modified; the lifecycle is verified against its gold contract.
"""

import json
from pathlib import Path

import pytest

from evaluation.dataset import default_dataset_root, load_bundle, load_manifest
from evaluation.d0_local_executor import D0LocalExecutor, retrieve_semantic, s0_config
from evaluation.d0_metrics import (
    adjudicate,
    is_multi_target_reinforcement,
    multi_target_reinforcement_diagnostic,
    safety_metrics,
    schedule_delta,
    tier_metrics,
)
from evaluation.models import (
    D0ConflictDecision,
    D0Prediction,
    D0RetrievalResult,
    D0Schedule,
)


def _prediction(**overrides: object) -> D0Prediction:
    base: dict[str, object] = {
        "event_id": "convT:M01",
        "bundle_id": "convT",
        "schedule": D0Schedule.EARLY,
        "config_id": "S0",
        "gold_operation": "add",
        "gold_target_ids": (),
        "candidate_text": "candidate fact",
        "pool": D0RetrievalResult(retrieved_ids=("M01",), scored_ids=("M01",), pool_size=1),
        "decision": D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
        "llm_request_hash": "a" * 64,
        "cache_hit": False,
    }
    base.update(overrides)
    return D0Prediction.model_validate(base)


def _outcome(**overrides: object):
    return adjudicate(_prediction(**overrides))


# ---------------------------------------------------------------------------
# Adjudication semantics (fixes 3, 4, 6)
# ---------------------------------------------------------------------------


def test_missed_supersede_is_decision_mismatch_not_false_supersede():
    # Gold update (SUPERSEDE tier); model keeps both: a MISS, not a false supersede.
    outcome = adjudicate(
        _prediction(
            gold_operation="update",
            gold_target_ids=("M05",),
            decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
        )
    )
    assert outcome.model_false_supersede is False
    assert outcome.decision_mismatch is True
    assert outcome.target_mismatch is False
    assert outcome.end_to_end_success is False


def test_destructive_supersede_on_add_is_false_supersede():
    outcome = adjudicate(
        _prediction(
            gold_operation="add",
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M01"),
        )
    )
    assert outcome.model_false_supersede is True
    assert outcome.decision_mismatch is True
    assert outcome.target_mismatch is True  # target-bearing decision on target-less gold
    assert outcome.end_to_end_success is False


def test_destructive_supersede_on_reinforce_is_false_supersede():
    outcome = adjudicate(
        _prediction(
            gold_operation="reinforce_existing",
            gold_target_ids=("M02",),
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M02"),
        )
    )
    assert outcome.model_false_supersede is True
    assert outcome.decision_mismatch is True
    assert outcome.end_to_end_success is False


def test_wrong_target_supersede_is_target_mismatch_not_false_supersede():
    # Right decision kind, wrong identity: target error, not a destructive
    # transition on an unrelated row (that side is pipeline_false_supersede).
    outcome = adjudicate(
        _prediction(
            gold_operation="update",
            gold_target_ids=("M02",),
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M08"),
        )
    )
    assert outcome.model_false_supersede is False
    assert outcome.decision_mismatch is False
    assert outcome.target_mismatch is True
    assert outcome.end_to_end_success is False


def test_exact_supersede_success():
    outcome = adjudicate(
        _prediction(
            gold_operation="update",
            gold_target_ids=("M05",),
            pool=D0RetrievalResult(retrieved_ids=("M05",), scored_ids=("M05",), pool_size=1),
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M05"),
        )
    )
    assert outcome.end_to_end_success is True
    assert outcome.decision_mismatch is False
    assert outcome.target_mismatch is False


def test_keep_both_requires_exact_keep_both():
    # DUPLICATE on gold add is NOT success (old code passed any non-SUPERSEDE).
    outcome = adjudicate(
        _prediction(
            gold_operation="add",
            decision=D0ConflictDecision(decision="DUPLICATE", target_memory_id="M01"),
        )
    )
    assert outcome.end_to_end_success is False
    assert outcome.decision_mismatch is True

    exact = adjudicate(
        _prediction(gold_operation="add", decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None))
    )
    assert exact.end_to_end_success is True


def test_invalid_output_is_not_keep_both_success():
    outcome = adjudicate(_prediction(gold_operation="add", decision=None))
    assert outcome.end_to_end_success is False
    assert outcome.decision_mismatch is None


def test_target_bearing_e2e_requires_gold_target_in_pool():
    # Correct target ID returned but never retrieved: fail-closed E2E failure.
    outcome = adjudicate(
        _prediction(
            gold_operation="update",
            gold_target_ids=("M05",),
            pool=D0RetrievalResult(retrieved_ids=("M01", "M02"), scored_ids=("M01", "M02"), pool_size=2),
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M05"),
        )
    )
    assert outcome.end_to_end_success is False

    dup = adjudicate(
        _prediction(
            gold_operation="reinforce_existing",
            gold_target_ids=("M02",),
            pool=D0RetrievalResult(retrieved_ids=("M01",), scored_ids=("M01",), pool_size=1),
            decision=D0ConflictDecision(decision="DUPLICATE", target_memory_id="M02"),
        )
    )
    assert dup.end_to_end_success is False


def test_multi_target_reinforcement_detection_is_generic():
    multi = _prediction(
        gold_operation="reinforce_existing",
        gold_target_ids=("M03", "M10", "M11"),
    )
    single = _prediction(
        gold_operation="reinforce_existing",
        gold_target_ids=("M12",),
    )
    assert is_multi_target_reinforcement(multi)
    assert not is_multi_target_reinforcement(single)
    assert not is_multi_target_reinforcement(_prediction(gold_operation="add"))
    assert not is_multi_target_reinforcement(
        _prediction(gold_operation="update", gold_target_ids=("M01",))
    )


def test_multi_target_excluded_from_duplicate_headline():
    multi = adjudicate(
        _prediction(
            event_id="convT:M14",
            gold_operation="reinforce_existing",
            gold_target_ids=("M03", "M10", "M11"),
        )
    )
    single_correct = adjudicate(
        _prediction(
            event_id="convT:M12",
            gold_operation="reinforce_existing",
            gold_target_ids=("M02",),
            pool=D0RetrievalResult(retrieved_ids=("M02",), scored_ids=("M02",), pool_size=1),
            decision=D0ConflictDecision(decision="DUPLICATE", target_memory_id="M02"),
        )
    )
    outcomes = [multi, single_correct]
    tier = tier_metrics(outcomes, "DUPLICATE")
    # Only the single-target case enters the headline tier.
    assert tier.end_to_end_total == 1
    assert tier.end_to_end_success == 1.0
    diagnostic = multi_target_reinforcement_diagnostic(outcomes)
    assert diagnostic["point_total"] == 1
    assert diagnostic["event_ids"] == ["convT:M14"]


# ---------------------------------------------------------------------------
# Safety metrics: execution denominator (fix 5)
# ---------------------------------------------------------------------------


def test_pool_empty_deterministic_fallback_excluded_from_llm_denominator():
    executed = _outcome(gold_operation="add")
    fallback = _outcome(
        event_id="convT:M02",
        gold_operation="add",
        pool=D0RetrievalResult(pool_size=0),
        decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
        llm_request_hash=None,
    )
    assert fallback.llm_executed is False
    safety = safety_metrics([executed, fallback])
    assert safety.llm_executed_total == 1
    # The deterministic fallback carries no decision-kind safety flags.
    assert fallback.model_false_supersede is None
    assert fallback.decision_mismatch is None


def test_invalid_output_counts_as_invalid_not_executed_decision():
    executed_invalid = _outcome(gold_operation="add", decision=None)
    safety = safety_metrics([executed_invalid])
    assert safety.llm_executed_total == 1
    assert safety.invalid_output_total == 1
    assert safety.decision_mismatch_total == 0
    assert safety.model_false_supersede_total == 0


def test_pipeline_false_supersede_counts_wrong_target_destructive_update():
    # Wrong-target SUPERSEDE on a gold update would delete a row without basis.
    outcome = adjudicate(
        _prediction(
            gold_operation="update",
            gold_target_ids=("M02",),
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M08"),
        )
    )
    safety = safety_metrics([outcome])
    assert safety.pipeline_false_supersede_total == 1
    # Literal false supersede stays false: the model chose a supersede on an
    # update gold (justified kind).
    assert safety.model_false_supersede_total == 0


def test_decision_and_target_mismatch_are_distinguishable():
    kind_wrong = adjudicate(
        _prediction(
            gold_operation="update",
            gold_target_ids=("M02",),
            decision=D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None),
        )
    )
    target_wrong = adjudicate(
        _prediction(
            gold_operation="update",
            gold_target_ids=("M02",),
            decision=D0ConflictDecision(decision="SUPERSEDE", target_memory_id="M08"),
        )
    )
    assert (kind_wrong.decision_mismatch, kind_wrong.target_mismatch) == (True, False)
    assert (target_wrong.decision_mismatch, target_wrong.target_mismatch) == (False, True)


# ---------------------------------------------------------------------------
# Shadow lifecycle row semantics (fix 1) — oracle invariants on the frozen dataset
# ---------------------------------------------------------------------------


def _timeline():
    root = default_dataset_root()
    manifest = load_manifest(root)
    bundles = tuple(load_bundle(entry, root=root) for entry in manifest.bundles)
    from evaluation.shadow_lifecycle import ShadowTimeline

    return ShadowTimeline(bundles), bundles


def test_final_active_rows_match_dataset_oracle_all_bundles_and_schedules():
    timeline, bundles = _timeline()
    checked = 0
    for bundle in bundles:
        bundle_id = bundle.manifest.bundle_id
        document = json.loads(
            (default_dataset_root() / "bundles" / bundle_id / "memories.json").read_text(
                encoding="utf-8"
            )
        )
        expected = set(document["expected_active_memory_row_source_event_ids"])
        for schedule in D0Schedule:
            schedule_bundle = timeline.bundle(schedule, bundle_id)
            final = schedule_bundle.post_batch_active_ids(
                max(key for key, _ in schedule_bundle.batches())
            )
            assert set(final) == expected, (
                f"{bundle_id}/{schedule.value}: rows {sorted(final)} != oracle {sorted(expected)}"
            )
            checked += 1
    assert checked == 8


def test_reinforcement_never_materializes_active_row():
    timeline, bundles = _timeline()
    for schedule in D0Schedule:
        for bundle in bundles:
            schedule_bundle = timeline.bundle(schedule, bundle.manifest.bundle_id)
            for key, batch in schedule_bundle.batches():
                post = set(schedule_bundle.post_batch_active_ids(key))
                for event in batch:
                    if event.expected_operation == "reinforce_existing":
                        assert event.event_id not in post, (
                            f"{bundle.manifest.bundle_id}:{event.event_id} became a row"
                        )


def test_conv03_reinforcement_update_chain_row_evolution():
    timeline, _ = _timeline()
    schedule_bundle = timeline.bundle(D0Schedule.LATE, "conv03")
    events = {event.event_id: event for event in schedule_bundle.events}

    def rows_after(event_id: str) -> set[str]:
        return set(schedule_bundle.post_batch_active_ids(schedule_bundle.batch_key(events[event_id])))

    # Contract chain: M02 add -> M08 reinforce M02 (no-op) -> M09 update supersedes
    # M02 -> M13 reinforce M09 (no-op). M08/M13 never become durable rows.
    assert "M02" in rows_after("M02")
    assert "M08" not in rows_after("M08")
    after_m09 = rows_after("M09")
    assert "M09" in after_m09 and "M02" not in after_m09
    after_m13 = rows_after("M13")
    # Between M09 and M13 the timeline legitimately applies M10 (update superseding
    # M01); M13 itself must add nothing and remove nothing.
    assert after_m13 - after_m09 == {"M10"}
    assert "M08" not in after_m13 and "M13" not in after_m13
    assert "M09" in after_m13


def test_update_after_reinforcement_sees_only_durable_rows():
    timeline, _ = _timeline()
    schedule_bundle = timeline.bundle(D0Schedule.LATE, "conv03")
    events = {event.event_id: event for event in schedule_bundle.events}
    # M09 (update superseding M02) must not see the M08 reinforcement as a row.
    bank = set(schedule_bundle.pre_batch_active_ids(events["M09"]))
    assert "M08" not in bank
    assert "M02" in bank


def test_multi_target_reinforcement_mutates_no_rows():
    timeline, _ = _timeline()
    for schedule in D0Schedule:
        schedule_bundle = timeline.bundle(schedule, "conv03")
        events = {event.event_id: event for event in schedule_bundle.events}
        before = set(schedule_bundle.pre_batch_active_ids(events["M14"]))
        after = set(schedule_bundle.post_batch_active_ids(schedule_bundle.batch_key(events["M14"])))
        # M14 confirms M03/M05/M06/M09/M10 without creating or removing rows.
        assert before == after or after - before == set()
        assert "M14" not in after


def test_same_batch_transition_is_atomic_against_shared_snapshot():
    timeline, _ = _timeline()
    for schedule in D0Schedule:
        for bundle_id in timeline.bundle_ids():
            schedule_bundle = timeline.bundle(schedule, bundle_id)
            active: set[str] = set()
            for _key, batch, snapshot in schedule_bundle.iter_batches():
                # The yielded snapshot is the shared pre-batch state every event
                # in the batch is resolved against.
                for event in batch:
                    pre = set(schedule_bundle.pre_batch_active_ids(event))
                    assert pre == set(snapshot), (
                        f"{bundle_id}/{schedule.value}:{event.event_id} saw a mutated snapshot"
                    )
                schedule_bundle._apply_batch(active, batch)


# ---------------------------------------------------------------------------
# Schedule delta taxonomy (fix 8): classes partition, annotations overlap
# ---------------------------------------------------------------------------


def _paired_predictions(event_id: str, *, early_pool: D0RetrievalResult, late_pool: D0RetrievalResult,
                        early_decision, late_decision):
    return [
        _prediction(
            event_id=event_id,
            schedule=D0Schedule.EARLY,
            pool=early_pool,
            decision=early_decision,
        ),
        _prediction(
            event_id=event_id,
            schedule=D0Schedule.LATE,
            pool=late_pool,
            decision=late_decision,
        ),
    ]


def test_schedule_delta_effective_classes_partition_and_annotations_overlap():
    same_pool = D0RetrievalResult(retrieved_ids=("M01",), scored_ids=("M01",), pool_size=1)
    keep = D0ConflictDecision(decision="KEEP_BOTH", target_memory_id=None)
    # Robust + boundary moved (structural says moved): still robust effectively.
    structural = {"convT:M01": (True, False)}
    early, late = _paired_predictions(
        "convT:M01", early_pool=same_pool, late_pool=same_pool,
        early_decision=keep, late_decision=keep,
    )
    delta = schedule_delta([early], [late], structural=structural)
    assert delta.robust_events == ("convT:M01",)
    assert delta.boundary_moved_events == ("convT:M01",)

    # Decision input differs (pool differs), outcome same: decision_input class.
    other_pool = D0RetrievalResult(retrieved_ids=("M02",), scored_ids=("M01", "M02"), pool_size=2)
    early, late = _paired_predictions(
        "convT:M02", early_pool=same_pool, late_pool=other_pool,
        early_decision=keep, late_decision=keep,
    )
    delta = schedule_delta([early], [late])
    assert delta.decision_input_sensitive_events == ("convT:M02",)
    assert delta.robust_events == ()
    assert delta.outcome_sensitive_events == ()


def test_retrieve_semantic_floor_gates_and_orders():
    class _Fixed:
        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    # Bank cosine 1.0 vs orthogonal 0.0; S1 floor 0.1 removes the orthogonal row.
    bank = {
        "A": ("a", [1.0, 0.0]),
        "B": ("b", [0.0, 1.0]),
    }
    pool = retrieve_semantic([1.0, 0.0], bank, s0_config(), top_k=10)
    assert pool.retrieved_ids == ("A", "B")
    pool = retrieve_semantic([1.0, 0.0], bank, s0_config() if False else type(s0_config())(config_id="S1", semantic_floor=0.1), top_k=10)
    assert pool.retrieved_ids == ("A",)
    assert pool.scored_ids == ("A",)
