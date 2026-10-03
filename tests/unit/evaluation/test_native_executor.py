"""Provider-free tests for native benchmark orchestration."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from evaluation.formation import (
    ExtractedFact,
    FormationExecutionStatus,
    FormationExtractionResult,
)
from evaluation.judge import JudgeError
from evaluation.models import (
    CaseEligibility,
    EvalCase,
    FormationInput,
    GoldFact,
    GoldSpecification,
    Message,
    Outcome,
    RetrievalInput,
    Suite,
)
from evaluation.native_executor import (
    NativeBenchmarkExecutor,
    NativeFormationEvaluator,
    NativeRetrievalEvaluator,
)
from evaluation.retrieval import RetrievalCaseEvaluation, RetrievalMode
from evaluation.scoring import (
    FormationMatchDecision,
    FormationMatchVerdict,
    JudgeProvenance,
    RetrievalScore,
)


def _formation_case(*, blocked: bool = False) -> EvalCase:
    return EvalCase(
        case_id="conv01:formation:M01",
        family_id="conv01:memory-family:preference",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        source_row_ids=("conv01:conversation:t1",),
        eligibility=(
            CaseEligibility(status="blocked", blocked_reasons=("pending_materialization",))
            if blocked
            else CaseEligibility()
        ),
        inputs=FormationInput(
            user_id="conv01:user",
            messages=(Message(message_id="conv01:t1", role="user", content="Ưu tiên Hà Nội"),),
        ),
        gold=GoldSpecification(
            facts=(
                GoldFact(
                    gold_id="conv01:M01",
                    text="Ưu tiên Hà Nội",
                    evidence_message_ids=("conv01:t1",),
                    attributed_to="user",
                ),
            ),
            semantic_expectation="Remember the location preference.",
        ),
    )


def _retrieval_case() -> EvalCase:
    return EvalCase(
        case_id="conv01:retrieval:Q01",
        family_id="conv01:scenario:single-hop",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        source_row_ids=("conv01:qa:Q01",),
        inputs=RetrievalInput(
            user_id="conv01:user",
            current_query="Ưu tiên ở đâu?",
        ),
        gold=GoldSpecification(
            relevant_memory_ids=("conv01:M01",),
            semantic_expectation="Retrieve the location preference.",
        ),
    )


class _FormationRuntime:
    def __init__(self, fact: str) -> None:
        self.fact = fact
        self.calls = 0

    async def evaluate(self, case: EvalCase) -> FormationExtractionResult:
        self.calls += 1
        return FormationExtractionResult(
            case_id=case.case_id,
            outcome=Outcome.REVIEW_REQUIRED,
            status=FormationExecutionStatus.VALID_FACTS,
            facts=(ExtractedFact(text=self.fact, attributed_to="user"),),
            provider_calls=1,
            stages=(),
        )


class _NeverJudge:
    async def formation(self, **_):
        raise AssertionError("exact formation match must not call the semantic judge")


def _judge_provenance() -> JudgeProvenance:
    return JudgeProvenance(
        provider="internal-judge",
        model="judge-model",
        prompt_sha256="a" * 64,
        response_schema_sha256="b" * 64,
    )


class _SemanticJudge:
    def __init__(self, verdict: FormationMatchVerdict) -> None:
        self.verdict = verdict

    async def formation(self, **kwargs):
        return (
            FormationMatchDecision(
                prediction_index=kwargs["prediction_indexes"][0],
                verdict=self.verdict,
                gold_id=(
                    next(iter(kwargs["gold_facts"]))
                    if self.verdict is FormationMatchVerdict.MATCH
                    else None
                ),
                reason_code="semantic_decision",
                judge=_judge_provenance(),
            ),
        )


class _FailingJudge:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def formation(self, **_):
        raise self.error


class _RetrievalRuntime:
    async def evaluate_gold(self, case: EvalCase) -> RetrievalCaseEvaluation:
        return RetrievalCaseEvaluation(
            case_id=case.case_id,
            mode=RetrievalMode.GOLD_FIXTURE,
            outcome=Outcome.PASS,
            score=RetrievalScore(recall_at_3=1, reciprocal_rank=1, first_relevant_rank=1),
            returned_memory_ids=(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),),
        )

    async def evaluate_formed(self, case: EvalCase) -> RetrievalCaseEvaluation:
        return RetrievalCaseEvaluation(
            case_id=case.case_id,
            mode=RetrievalMode.FORMATION_PRODUCED,
            outcome=Outcome.FAIL,
            score=RetrievalScore(recall_at_3=0, reciprocal_rank=0),
        )


async def test_native_formation_exact_match_scores_without_judge():
    runtime = _FormationRuntime("Ưu tiên Hà Nội")
    result = await NativeFormationEvaluator(runtime, _NeverJudge()).evaluate(_formation_case())  # type: ignore[arg-type]

    assert result.outcome is Outcome.PASS
    assert result.score is not None and result.score.f1 == 1
    assert result.judge_decisions == ()
    assert runtime.calls == 1


async def test_native_formation_semantic_match_and_uncertain_paths():
    matched = await NativeFormationEvaluator(
        _FormationRuntime("Ưu tiên tại Hà Nội"),
        _SemanticJudge(FormationMatchVerdict.MATCH),  # type: ignore[arg-type]
    ).evaluate(_formation_case())
    assert matched.outcome is Outcome.PASS
    assert matched.score is not None and matched.score.f1 == 1

    uncertain = await NativeFormationEvaluator(
        _FormationRuntime("Có thể ưu tiên miền Bắc"),
        _SemanticJudge(FormationMatchVerdict.UNCERTAIN),  # type: ignore[arg-type]
    ).evaluate(_formation_case())
    assert uncertain.outcome is Outcome.REVIEW_REQUIRED
    assert uncertain.reason_codes == ("formation_semantic_uncertain",)


async def test_native_formation_fails_closed_when_judge_fails():
    dependency = await NativeFormationEvaluator(
        _FormationRuntime("semantic candidate"),
        _FailingJudge(JudgeError(Outcome.DEPENDENCY_ERROR, "judge_unavailable")),  # type: ignore[arg-type]
    ).evaluate(_formation_case())
    assert dependency.outcome is Outcome.DEPENDENCY_ERROR
    assert dependency.reason_codes == ("judge_unavailable",)

    unexpected = await NativeFormationEvaluator(
        _FormationRuntime("semantic candidate"),
        _FailingJudge(RuntimeError("sensitive provider text")),  # type: ignore[arg-type]
    ).evaluate(_formation_case())
    assert unexpected.outcome is Outcome.PROTOCOL_ERROR
    assert unexpected.reason_codes == ("judge_unexpected_error",)


async def test_open_world_formation_judge_gets_source_boundary_and_qualified_context():
    base = _formation_case()
    case = base.model_copy(
        update={
            "inputs": FormationInput(
                user_id="conv01:user",
                messages=(
                    Message(message_id="conv01:prior", role="user", content="Ngưỡng 99%"),
                    Message(message_id="conv01:t1", role="user", content="Quay lại ngưỡng 98%"),
                ),
                source_message_ids=("conv01:t1",),
            ),
            "gold": GoldSpecification(
                formation_contract="open_world",
                forbidden_facts=("Một mật khẩu",),
                semantic_expectation="Keep threshold reassertion as event evidence.",
            ),
        }
    )

    class SourceJudge:
        async def formation(self, **kwargs):
            assert kwargs["contract"] == "open_world"
            assert kwargs["source_messages"][0]["content"] == "Quay lại ngưỡng 98%"
            assert kwargs["context_messages"][0]["content"] == "Ngưỡng 99%"
            assert kwargs["prediction_details"][0]["attributed_to"] == "user"
            assert kwargs["forbidden_facts"] == ("Một mật khẩu",)
            return (
                FormationMatchDecision(
                    prediction_index=0,
                    verdict=FormationMatchVerdict.VALID_EXTRA,
                    reason_code="source_reassertion",
                    judge=_judge_provenance(),
                ),
            )

    result = await NativeFormationEvaluator(
        _FormationRuntime("Ngưỡng 98%"),
        SourceJudge(),  # type: ignore[arg-type]
    ).evaluate(case)
    assert result.outcome is Outcome.PASS
    assert result.score is not None and result.score.valid_extra == 1
    assert result.score.false_positive == 0


async def test_native_exact_match_keeps_semantic_credit_for_assistant_reinforcement():
    class AssistantReinforcementRuntime(_FormationRuntime):
        async def evaluate(self, case):
            result = await super().evaluate(case)
            return result.model_copy(
                update={"facts": (ExtractedFact(text=self.fact, attributed_to="assistant"),)}
            )

    result = await NativeFormationEvaluator(
        AssistantReinforcementRuntime("Ưu tiên Hà Nội"),
        _NeverJudge(),  # type: ignore[arg-type]
    ).evaluate(_formation_case())
    # Canonical M02 explicitly accepts assistant reinforcement of a user's stated preference;
    # the compiler's generic attributed_to=user must not create a new hard gate against that.
    assert result.outcome is Outcome.PASS
    assert result.score.true_positive == 1


async def test_native_retrieval_keeps_both_modes_and_combines_quality_failure():
    result = await NativeRetrievalEvaluator(_RetrievalRuntime()).evaluate(_retrieval_case())

    assert result.outcome is Outcome.FAIL
    assert result.gold_fixture is not None
    assert result.gold_fixture.outcome is Outcome.PASS
    assert result.formation_produced is not None
    assert result.formation_produced.outcome is Outcome.FAIL


async def test_native_dispatch_blocks_before_runtime_and_fails_closed_on_identity_drift():
    runtime = _FormationRuntime("Ưu tiên Hà Nội")
    executor = NativeBenchmarkExecutor(
        {Suite.FORMATION: NativeFormationEvaluator(runtime, _NeverJudge())}  # type: ignore[arg-type]
    )

    blocked = await executor.evaluate(_formation_case(blocked=True))
    assert blocked.outcome is Outcome.NOT_RUN
    assert runtime.calls == 0

    class WrongIdentity:
        async def evaluate(self, _case):
            return SimpleNamespace(
                case_id="another:case",
                outcome=Outcome.PASS,
                reason_codes=(),
            )

    drift = await NativeBenchmarkExecutor(  # type: ignore[arg-type]
        {Suite.FORMATION: WrongIdentity()}
    ).evaluate(_formation_case())
    assert drift.outcome is Outcome.PROTOCOL_ERROR
    assert drift.reason_codes == ("native_case_identity_mismatch",)

    missing = await executor.evaluate(_retrieval_case())
    assert missing.outcome is Outcome.PROTOCOL_ERROR
    assert missing.reason_codes == ("native_suite_not_configured",)


def test_native_contracts_reject_invalid_state_and_empty_dispatch():
    from evaluation.native_executor import (
        NativeFormationCaseEvaluation,
        NativeRetrievalCaseEvaluation,
    )

    with pytest.raises(ValueError, match="at least one"):
        NativeBenchmarkExecutor({})
    with pytest.raises(ValueError, match="unique"):
        NativeFormationCaseEvaluation(
            case_id="case",
            outcome=Outcome.PROTOCOL_ERROR,
            reason_codes=("same", "same"),
        )
    with pytest.raises(ValueError, match="both corpus"):
        NativeRetrievalCaseEvaluation(case_id="case", outcome=Outcome.PASS)
