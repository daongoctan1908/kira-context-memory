"""TC-1..TC-14 from docs/evaluator-scope-semantics.md — scope gates on formation scoring."""

from datetime import UTC, datetime

import pytest

from evaluation.formation import ExtractedFact, FormationLifecycleRecord
from evaluation.models import (
    CaseEligibility,
    EvalCase,
    FormationInput,
    FormationLifecycleGold,
    GoldFact,
    GoldSpecification,
    Message,
    Outcome,
)
from evaluation.native_executor import NativeFormationEvaluator
from evaluation.scoring import classify_scope, score_scope_semantics

# --- classify_scope mirrors vendored _enforce_memory_scopes -------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "MISSING"),
        ("", "MISSING"),
        ("   ", "MISSING"),
        ("conversation", "CONVERSATION"),
        (" global ", "GLOBAL"),
        ("GALAXY", "INVALID"),
        (123, "INVALID"),
    ],
)
def test_classify_scope_mirrors_vendored_enforcement(raw, expected):
    assert classify_scope(raw) == expected


# --- score_scope_semantics unit contracts ------------------------------------


def test_scope_score_persisted_gates_and_metrics():
    semantics = score_scope_semantics(
        negative=False,
        gold_scope="CONVERSATION",
        matched_pairs=[("CONVERSATION", True), ("GLOBAL", True), ("MISSING", True)],
    )
    assert semantics.false_global_promotion
    assert not semantics.persistence_miss
    assert semantics.fallback_count == 1


def test_scope_score_persistence_miss_requires_unpersisted_pair():
    semantics = score_scope_semantics(
        negative=False,
        gold_scope="GLOBAL",
        matched_pairs=[("CONVERSATION", False)],
    )
    assert semantics.persistence_miss
    assert semantics.missed_global_count == 1


def test_scope_score_negative_only_counts_diagnostics():
    semantics = score_scope_semantics(
        negative=True,
        negative_predictions=("GLOBAL", "INVALID", "MISSING", "CONVERSATION"),
    )
    assert not semantics.false_global_promotion
    assert not semantics.persistence_miss
    assert semantics.missed_global_count == 1
    assert semantics.fallback_count == 1
    assert semantics.invalid_count == 1


# --- end-to-end through NativeFormationEvaluator -----------------------------


class _Runtime:
    def __init__(self, facts, lifecycle_texts):
        self.facts = tuple(facts)
        self.lifecycle_texts = tuple(lifecycle_texts)

    async def evaluate(self, case: EvalCase):
        from evaluation.formation import (
            FormationExecutionStatus,
            FormationExtractionResult,
        )

        return FormationExtractionResult(
            case_id=case.case_id,
            outcome=Outcome.REVIEW_REQUIRED,
            status=(
                FormationExecutionStatus.VALID_FACTS
                if self.facts
                else FormationExecutionStatus.VALID_EMPTY
            ),
            facts=self.facts,
            lifecycle_events=tuple(
                FormationLifecycleRecord(event="ADD", memory_id=_uuid_for(i), memory=text)
                for i, text in enumerate(self.lifecycle_texts)
            ),
            provider_calls=1,
            stages=(),
        )


def _uuid_for(index: int):
    from uuid import UUID

    return UUID(f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}")


def _case(*, gold_scope=None, should_store=True) -> EvalCase:
    text = "Ưu tiên Hà Nội" if should_store else None
    facts = ()
    if should_store:
        facts = (
            GoldFact(
                gold_id="conv01:M01",
                text=text,
                evidence_message_ids=("conv01:t1",),
                attributed_to="user",
            ),
        )
    lifecycle = FormationLifecycleGold(
        event_id="conv01:M01",
        should_store=should_store,
        expected_operation="add" if should_store else "do_not_persist",
        active_at_end=should_store,
        memory_family="preference",
        memory_scope=gold_scope,
    )
    return EvalCase(
        case_id="conv01:formation:M01",
        family_id="conv01:memory-family:preference",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        source_row_ids=("conv01:conversation:t1",),
        eligibility=CaseEligibility(),
        inputs=FormationInput(
            user_id="conv01:user",
            messages=(Message(message_id="conv01:t1", role="user", content="Ưu tiên Hà Nội"),),
        ),
        gold=GoldSpecification(
            facts=facts,
            semantic_expectation=text or "Do not persist the unsupported candidate conv01:M01",
            lifecycle_event=lifecycle,
        ),
    )


def _fact(text, scope) -> ExtractedFact:
    return ExtractedFact(text=text, attributed_to="user", scope=scope)


def _add(text) -> FormationLifecycleRecord:
    from uuid import uuid5

    return FormationLifecycleRecord(
        event="ADD",
        memory_id=uuid5(_NAMESPACE, text),
        memory=text,
    )


from uuid import NAMESPACE_URL as _NAMESPACE  # noqa: E402

_NOW = datetime(2026, 9, 25, tzinfo=UTC)


async def _evaluate(case, facts, lifecycle_texts):
    evaluator = NativeFormationEvaluator(_Runtime(facts, lifecycle_texts), _NeverJudge())  # type: ignore[arg-type]
    return await evaluator.evaluate(case)


class _NeverJudge:
    async def formation(self, **_):
        raise AssertionError("exact formation match must not call the semantic judge")


# TC-1: matched gold CONVERSATION + predicted CONVERSATION → PASS
async def test_tc1_conversation_pair_passes():
    result = await _evaluate(
        _case(gold_scope="CONVERSATION"),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="CONVERSATION")],
        ["Ưu tiên Hà Nội"],
    )
    assert result.outcome is Outcome.PASS
    assert result.scope_semantics is not None
    assert not result.scope_semantics.false_global_promotion
    assert result.reason_codes == ()


# TC-2: matched gold GLOBAL + predicted GLOBAL → PASS
async def test_tc2_global_pair_passes():
    result = await _evaluate(
        _case(gold_scope="GLOBAL"),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="GLOBAL")],
        ["Ưu tiên Hà Nội"],
    )
    assert result.outcome is Outcome.PASS


# TC-3: gold CONVERSATION predicted GLOBAL → FAIL promotion gate
async def test_tc3_false_global_promotion_fails():
    result = await _evaluate(
        _case(gold_scope="CONVERSATION"),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="GLOBAL")],
        ["Ưu tiên Hà Nội"],
    )
    assert result.outcome is Outcome.FAIL
    assert "scope_false_global_promotion" in result.reason_codes


# TC-4: gold GLOBAL predicted CONVERSATION → PASS case, missed_global +1
async def test_tc4_missed_global_is_metric_not_gate():
    result = await _evaluate(
        _case(gold_scope="GLOBAL"),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="CONVERSATION")],
        ["Ưu tiên Hà Nội"],
    )
    assert result.outcome is Outcome.PASS
    assert result.scope_semantics is not None
    assert result.scope_semantics.missed_global_count == 1


# TC-5: gold GLOBAL predicted MISSING → missed_global + fallback
async def test_tc5_missing_scope_counts_fallback_and_missed_global():
    result = await _evaluate(
        _case(gold_scope="GLOBAL"),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope=None)],
        ["Ưu tiên Hà Nội"],
    )
    assert result.outcome is Outcome.PASS
    assert result.scope_semantics is not None
    assert result.scope_semantics.missed_global_count == 1
    assert result.scope_semantics.fallback_count == 1


# TC-6: predicted INVALID without ADD → persistence miss gate
async def test_tc6_invalid_scope_without_add_fails_persistence():
    result = await _evaluate(
        _case(gold_scope="CONVERSATION"),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="GALAXY")],
        [],
    )
    assert result.outcome is Outcome.FAIL
    assert "formation_persistence_miss" in result.reason_codes
    assert result.scope_semantics is not None
    assert result.scope_semantics.invalid_count == 1


# TC-7: matched text without an ADD lifecycle (e.g. dedup suppressed the write) → persistence miss
async def test_tc7_deduped_write_still_persistence_miss():
    result = await _evaluate(
        _case(gold_scope="CONVERSATION"),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="CONVERSATION")],
        [],
    )
    assert result.outcome is Outcome.FAIL
    assert "formation_persistence_miss" in result.reason_codes


# TC-8: negative gold, no predictions → PASS
async def test_tc8_negative_with_no_predictions_passes():
    result = await _evaluate(_case(should_store=False), [], [])
    assert result.outcome is Outcome.PASS
    assert result.scope_semantics is not None
    assert not result.scope_semantics.false_global_promotion


# TC-9: negative gold, unrelated prediction → FAIL false-ADD
async def test_tc9_negative_unrelated_prediction_fails_false_add():
    fact = ExtractedFact(
        text="Chi Tiên chỉ là hỗ trợ tạm", attributed_to="user", scope="CONVERSATION"
    )
    result = await _evaluate(
        _case(should_store=False),
        [fact],
        ["Chi Tiên chỉ là hỗ trợ tạm"],
    )
    assert result.outcome is Outcome.FAIL
    assert "formation_quality_mismatch" in result.reason_codes


# TC-10: negative gold, prediction matching canonical_fact → FAIL false-ADD, no promotion
async def test_tc10_negative_matching_prediction_fails_without_promotion():
    result = await _evaluate(
        _case(should_store=False),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="GLOBAL")],
        ["Ưu tiên Hà Nội"],
    )
    assert result.outcome is Outcome.FAIL
    assert "formation_quality_mismatch" in result.reason_codes
    assert "scope_false_global_promotion" not in result.reason_codes


# TC-11: negative gold, predicted GLOBAL → false-ADD + diagnostic counter
async def test_tc11_negative_global_prediction_counts_diagnostic():
    result = await _evaluate(
        _case(should_store=False),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="GLOBAL")],
        ["Ưu tiên Hà Nội"],
    )
    assert result.outcome is Outcome.FAIL
    assert result.scope_semantics is not None
    assert result.scope_semantics.missed_global_count == 1


# TC-12: negative gold, predicted INVALID → still false-ADD (any extraction violates the negative)
async def test_tc12_negative_invalid_scope_still_false_add():
    result = await _evaluate(
        _case(should_store=False),
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="GALAXY")],
        [],
    )
    assert result.outcome is Outcome.FAIL
    assert "formation_quality_mismatch" in result.reason_codes
    assert result.scope_semantics is not None
    assert result.scope_semantics.invalid_count == 1


# TC-13: persistent path — payload scope feeds the promotion gate
async def test_tc13_persistent_payload_scope_gates_promotion():
    from uuid import UUID

    from evaluation.formation import FormationPersistenceSnapshot, PersistedFormationMemory

    class _PersistentRuntime(_Runtime):
        async def evaluate(self, case: EvalCase):
            from evaluation.formation import (
                FormationReceiptRecord,
                PersistentFormationResult,
            )

            extraction = await super().evaluate(case)
            memory = PersistedFormationMemory(
                memory_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-000000000001"),
                content="Ưu tiên Hà Nội",
                user_id="conv01:user",
                formation_event_id=UUID("cccccccc-cccc-4ccc-8ccc-000000000001"),
                conversation_id=UUID("dddddddd-dddd-4ddd-8ddd-000000000001"),
                turn_id="conv01:t1",
                boundary_message_id=1,
                memory_scope="GLOBAL",
            )
            receipt = FormationReceiptRecord(
                event_id=memory.formation_event_id,
                user_id=memory.user_id,
                conversation_id=memory.conversation_id,
                events=extraction.lifecycle_events,
                memory_count=len(extraction.lifecycle_events),
                committed_at=extraction.created_at if hasattr(extraction, "created_at") else _NOW,
            )
            return PersistentFormationResult(
                case_id=case.case_id,
                family_id=case.family_id,
                logical_user_id=case.inputs.user_id,
                persisted_user_id=case.inputs.user_id,
                outcome=Outcome.REVIEW_REQUIRED,
                extraction=extraction,
                persistence=FormationPersistenceSnapshot(
                    event_id=memory.formation_event_id,
                    user_id=memory.user_id,
                    memories=(memory,),
                    receipt=receipt,
                ),
            )

    evaluator = NativeFormationEvaluator(_PersistentRuntime(
        [ExtractedFact(text="Ưu tiên Hà Nội", attributed_to="user", scope="CONVERSATION")],
        ["Ưu tiên Hà Nội"],
    ), _NeverJudge())  # type: ignore[arg-type]
    result = await evaluator.evaluate(_case(gold_scope="CONVERSATION"))
    assert result.outcome is Outcome.FAIL
    assert "scope_false_global_promotion" in result.reason_codes


# TC-14: judge-matched pair still gets scope-gated
async def test_tc14_judge_matched_pair_scope_gated():
    class _SemanticJudge:
        async def formation(self, **kwargs):
            from evaluation.scoring import (
                FormationMatchDecision,
                FormationMatchVerdict,
                JudgeProvenance,
            )

            return (
                FormationMatchDecision(
                    prediction_index=kwargs["prediction_indexes"][0],
                    verdict=FormationMatchVerdict.MATCH,
                    gold_id=next(iter(kwargs["gold_facts"])),
                    reason_code="semantic_decision",
                    judge=JudgeProvenance(
                        provider="internal-judge",
                        model="judge-model",
                        prompt_sha256="a" * 64,
                        response_schema_sha256="b" * 64,
                    ),
                ),
            )

    evaluator = NativeFormationEvaluator(
        _Runtime(
            [ExtractedFact(text="Ưu tiên tại Hà Nội", attributed_to="user", scope="GLOBAL")],
            ["Ưu tiên tại Hà Nội"],
        ),
        _SemanticJudge(),
    )
    result = await evaluator.evaluate(_case(gold_scope="CONVERSATION"))
    assert result.outcome is Outcome.FAIL
    assert "scope_false_global_promotion" in result.reason_codes
