"""Paired cross-session orchestration and stage-scoring contracts."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from evaluation.cross_session import (
    CrossSessionCondition,
    CrossSessionDependencyError,
    CrossSessionEvaluator,
    MemoryJobHandle,
    MemoryReadiness,
    MemoryReadinessStatus,
    RetrievedMemory,
    SessionBExecution,
)
from evaluation.judge import JudgeError
from evaluation.models import (
    CaseEligibility,
    CrossSessionInput,
    EvalCase,
    GoldSpecification,
    Message,
    Outcome,
    Profile,
    Suite,
)
from evaluation.scoring import JudgeProvenance, JudgeVerdict, SemanticJudgment, output_sha256

_EVENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _case() -> EvalCase:
    return EvalCase(
        case_id="conv01:cross-session:q1",
        family_id="conv01:scenario:recall",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        source_row_ids=("conv01:turn:t1", "conv01:qa:q1"),
        inputs=CrossSessionInput(
            user_id="synthetic-user",
            session_a="session-a",
            session_b="session-b",
            session_a_messages=(
                Message(
                    message_id="turn-1-user",
                    session_id="session-a",
                    role="user",
                    content="Ngưỡng FTTH là 95%.",
                    timestamp=datetime(2026, 9, 18, tzinfo=UTC),
                ),
            ),
            session_b_query="Ngưỡng tôi đã đặt là bao nhiêu?",
            source_session_ids=("session-a",),
        ),
        gold=GoldSpecification(
            relevant_memory_ids=("gold-1",),
            required_exact=("95%",),
            forbidden=("90%",),
            semantic_expectation="State that the configured threshold is 95%.",
            expected_rewrite="Ngưỡng FTTH tôi đã đặt là bao nhiêu?",
            expected_answer="Ngưỡng FTTH đã đặt là 95%.",
            expected_action="answer",
            expected_api={"metric": "FTTH"},
        ),
    )


def _execution(
    condition: CrossSessionCondition,
    *,
    provider: str = "kira-internal-v1",
    query: str = "Ngưỡng tôi đã đặt là bao nhiêu?",
    user_id: str = "synthetic-user",
    session_id: str = "session-b",
    memories: tuple[RetrievedMemory, ...] | None = None,
    rewrite: str = "Ngưỡng FTTH tôi đã đặt là 95%?",
    answer: str = "Ngưỡng FTTH đã đặt là 95%.",
    action: str | None = "answer",
    api: dict[str, object] | None = None,
    event_id: UUID | None = None,
) -> SessionBExecution:
    if memories is None:
        memories = (
            (
                RetrievedMemory(
                    memory_id="memory-1",
                    user_id="synthetic-user",
                    score=0.9,
                ),
            )
            if condition is CrossSessionCondition.WITH_LTM
            else ()
        )
    return SessionBExecution(
        condition=condition,
        user_id=user_id,
        session_id=session_id,
        current_query=query,
        provider_id=provider,
        retrieved_memories=memories,
        rewritten_query=rewrite,
        final_answer=answer,
        actual_action=action,
        actual_api={"metric": "FTTH", "extra": "allowed"} if api is None else api,
        query_memory_event_id=event_id,
    )


class _Runtime:
    def __init__(
        self,
        *,
        readiness: MemoryReadinessStatus = MemoryReadinessStatus.COMPLETED,
        no_ltm: SessionBExecution | None = None,
        with_ltm: SessionBExecution | None = None,
        error: Exception | None = None,
    ) -> None:
        self.readiness = readiness
        self.no_ltm = no_ltm or _execution(CrossSessionCondition.NO_LTM)
        self.with_ltm = with_ltm or _execution(CrossSessionCondition.WITH_LTM)
        self.error = error
        self.calls: list[object] = []

    async def persist_session_a(self, case: EvalCase) -> MemoryJobHandle:
        self.calls.append(("persist", case.case_id))
        if self.error is not None:
            raise self.error
        return MemoryJobHandle(event_id=_EVENT_ID)

    async def wait_for_memory(self, handle, *, timeout_seconds):
        self.calls.append(("wait", handle.event_id, timeout_seconds))
        return MemoryReadiness(
            event_id=handle.event_id,
            status=self.readiness,
            gold_ids_by_memory_id=(
                {"memory-1": ("gold-1",)}
                if self.readiness is MemoryReadinessStatus.COMPLETED
                else {}
            ),
        )

    async def run_session_b(
        self,
        case,
        *,
        condition,
        enable_ltm,
        schedule_memory,
    ):
        self.calls.append(("run", condition, enable_ltm, schedule_memory, case.case_id))
        return self.no_ltm if condition is CrossSessionCondition.NO_LTM else self.with_ltm


def _provenance() -> JudgeProvenance:
    return JudgeProvenance(
        provider="internal_openai_compatible",
        model="judge-model",
        prompt_sha256="a" * 64,
        response_schema_sha256="b" * 64,
    )


class _Judge:
    def __init__(
        self,
        verdicts: tuple[JudgeVerdict, ...] = (),
        error_at: int | None = None,
    ) -> None:
        self.verdicts = verdicts
        self.error_at = error_at
        self.calls: list[dict[str, object]] = []

    async def semantic(self, **kwargs):
        self.calls.append(kwargs)
        if self.error_at == len(self.calls):
            raise JudgeError(Outcome.DEPENDENCY_ERROR, "judge_timeout")
        verdict = (
            self.verdicts[len(self.calls) - 1]
            if len(self.calls) <= len(self.verdicts)
            else JudgeVerdict.PASS
        )
        return SemanticJudgment(
            case_id=str(kwargs["case_id"]),
            suite=Suite.CROSS_SESSION,
            output_sha256=output_sha256(kwargs["output"]),
            verdict=verdict,
            reason_code={
                JudgeVerdict.PASS: "semantic_equivalent",
                JudgeVerdict.FAIL: "semantic_mismatch",
                JudgeVerdict.UNCERTAIN: "semantic_uncertain",
            }[verdict],
            rationale="The output was judged against the supplied benchmark reference.",
            judge=_provenance(),
        )


def _evaluator(runtime: _Runtime, judge: _Judge | None = None) -> CrossSessionEvaluator:
    return CrossSessionEvaluator(
        runtime,
        judge or _Judge(),
        profile=Profile.MOCK,
        backend="mock",
        readiness_timeout_seconds=5,
    )


@pytest.mark.asyncio
async def test_required_flow_runs_formation_then_paired_session_b_without_writes():
    runtime = _Runtime()
    judge = _Judge()

    result = await _evaluator(runtime, judge).evaluate(_case())

    assert result.outcome is Outcome.PASS
    assert result.event_id == _EVENT_ID
    assert result.readiness is MemoryReadinessStatus.COMPLETED
    assert result.no_ltm is not None and result.no_ltm.retrieval is None
    assert result.with_ltm is not None and result.with_ltm.retrieval is not None
    assert result.with_ltm.retrieval.recall_at_3 == 1
    assert result.with_ltm.retrieval.reciprocal_rank == 1
    assert result.no_ltm.final_judgment is not None
    assert result.with_ltm.final_judgment is not None
    assert runtime.calls == [
        ("persist", "conv01:cross-session:q1"),
        ("wait", _EVENT_ID, 5.0),
        (
            "run",
            CrossSessionCondition.NO_LTM,
            False,
            False,
            "conv01:cross-session:q1",
        ),
        (
            "run",
            CrossSessionCondition.WITH_LTM,
            True,
            False,
            "conv01:cross-session:q1",
        ),
    ]
    assert len(judge.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (MemoryReadinessStatus.DEAD, "memory_job_dead"),
        (MemoryReadinessStatus.TIMEOUT, "memory_readiness_timeout"),
    ],
)
async def test_dead_or_timed_out_formation_is_dependency_error_not_retrieval_miss(status, reason):
    runtime = _Runtime(readiness=status)

    result = await _evaluator(runtime).evaluate(_case())

    assert result.outcome is Outcome.DEPENDENCY_ERROR
    assert result.reason_codes == (reason,)
    assert result.no_ltm is None and result.with_ltm is None
    assert not any(call[0] == "run" for call in runtime.calls)


@pytest.mark.asyncio
async def test_retrieval_miss_and_semantic_final_failure_are_reported_separately():
    runtime = _Runtime(
        with_ltm=_execution(CrossSessionCondition.WITH_LTM, memories=()),
    )
    judge = _Judge(
        verdicts=(
            JudgeVerdict.PASS,
            JudgeVerdict.PASS,
            JudgeVerdict.PASS,
            JudgeVerdict.FAIL,
        )
    )

    result = await _evaluator(runtime, judge).evaluate(_case())

    assert result.outcome is Outcome.FAIL
    assert result.with_ltm is not None
    assert result.with_ltm.retrieval is not None
    assert result.with_ltm.retrieval.recall_at_3 == 0
    assert result.with_ltm.final_judgment is not None
    assert result.with_ltm.final_judgment.verdict is JudgeVerdict.FAIL


@pytest.mark.asyncio
async def test_pair_drift_scope_leak_and_query_write_are_hard_failures():
    runtime = _Runtime(
        no_ltm=_execution(CrossSessionCondition.NO_LTM, provider="provider-a"),
        with_ltm=_execution(
            CrossSessionCondition.WITH_LTM,
            provider="provider-b",
            user_id="another-user",
            event_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ),
    )

    result = await _evaluator(runtime).evaluate(_case())

    assert result.outcome is Outcome.FAIL
    assert result.with_ltm is not None
    assert set(result.with_ltm.safety_violation_codes) == {
        "cross_session_pair_provider_drift",
        "cross_session_scope_mismatch",
        "session_b_corpus_contamination",
    }


@pytest.mark.asyncio
async def test_foreign_or_cross_user_retrieval_is_a_safety_failure():
    runtime = _Runtime(
        with_ltm=_execution(
            CrossSessionCondition.WITH_LTM,
            memories=(
                RetrievedMemory(
                    memory_id="foreign-memory",
                    user_id="another-user",
                    score=0.8,
                ),
            ),
        )
    )

    result = await _evaluator(runtime).evaluate(_case())

    assert result.with_ltm is not None
    assert result.with_ltm.outcome is Outcome.FAIL
    assert set(result.with_ltm.safety_violation_codes) == {
        "cross_session_user_leak",
        "cross_session_foreign_memory",
    }


@pytest.mark.asyncio
async def test_rewrite_constraint_failure_skips_only_rewrite_judge_not_final_qa():
    runtime = _Runtime(
        with_ltm=_execution(
            CrossSessionCondition.WITH_LTM,
            rewrite="Ngưỡng FTTH là 90%?",
        )
    )
    judge = _Judge()

    result = await _evaluator(runtime, judge).evaluate(_case())

    assert result.with_ltm is not None
    assert result.with_ltm.outcome is Outcome.FAIL
    assert result.with_ltm.rewrite_judgment is None
    assert result.with_ltm.final_judgment is not None
    assert "rewrite_constraint_failed" in result.with_ltm.reason_codes
    assert len(judge.calls) == 3


@pytest.mark.asyncio
async def test_judge_failure_is_dependency_error_not_semantic_fail():
    result = await _evaluator(_Runtime(), _Judge(error_at=4)).evaluate(_case())

    assert result.outcome is Outcome.DEPENDENCY_ERROR
    assert result.with_ltm is not None
    assert result.with_ltm.final_judgment is None
    assert "judge_timeout" in result.with_ltm.reason_codes


@pytest.mark.asyncio
async def test_uncertain_final_answer_requires_review_without_becoming_failure():
    judge = _Judge(
        verdicts=(
            JudgeVerdict.PASS,
            JudgeVerdict.PASS,
            JudgeVerdict.PASS,
            JudgeVerdict.UNCERTAIN,
        )
    )

    result = await _evaluator(_Runtime(), judge).evaluate(_case())

    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert result.with_ltm is not None
    assert result.with_ltm.outcome is Outcome.REVIEW_REQUIRED
    assert result.with_ltm.final_judgment is not None
    assert result.with_ltm.final_judgment.verdict is JudgeVerdict.UNCERTAIN


@pytest.mark.asyncio
async def test_structured_task_is_na_when_runtime_does_not_expose_it():
    runtime = _Runtime(
        no_ltm=_execution(CrossSessionCondition.NO_LTM, action=None),
        with_ltm=_execution(CrossSessionCondition.WITH_LTM, action=None),
    )

    result = await _evaluator(runtime).evaluate(_case())

    assert result.outcome is Outcome.PASS
    assert result.no_ltm is not None and result.no_ltm.task_success is None
    assert result.with_ltm is not None and result.with_ltm.task_success is None
    assert result.no_ltm.reason_codes == ("task_outcome_unobservable",)


@pytest.mark.asyncio
async def test_structured_task_mismatch_fails_without_becoming_semantic_judgment():
    runtime = _Runtime(
        with_ltm=_execution(
            CrossSessionCondition.WITH_LTM,
            action="answer",
            api={"metric": "5G"},
        )
    )

    result = await _evaluator(runtime).evaluate(_case())

    assert result.with_ltm is not None
    assert result.with_ltm.task_success is not None
    assert not result.with_ltm.task_success.passed
    assert result.with_ltm.task_success.mismatch_paths == ("api.metric",)
    assert result.with_ltm.final_judgment is not None


@pytest.mark.asyncio
async def test_blocked_case_and_dependency_error_never_create_fake_pair():
    blocked = _case().model_copy(
        update={
            "eligibility": CaseEligibility(
                status="blocked",
                blocked_reasons=("pending_kira_final_answer",),
            )
        }
    )
    blocked_runtime = _Runtime()
    blocked_result = await _evaluator(blocked_runtime).evaluate(blocked)
    assert blocked_result.outcome is Outcome.NOT_RUN
    assert blocked_runtime.calls == []

    failed = await _evaluator(
        _Runtime(error=CrossSessionDependencyError()),
    ).evaluate(_case())
    assert failed.outcome is Outcome.DEPENDENCY_ERROR
    assert failed.no_ltm is None and failed.with_ltm is None


class _CancelledRuntime(_Runtime):
    async def run_session_b(self, *args, **kwargs):
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_cancellation_propagates_and_does_not_create_a_result():
    with pytest.raises(asyncio.CancelledError):
        await _evaluator(_CancelledRuntime()).evaluate(_case())


class _SlowReadinessRuntime(_Runtime):
    async def wait_for_memory(self, handle, *, timeout_seconds):
        await asyncio.sleep(1)
        return await super().wait_for_memory(handle, timeout_seconds=timeout_seconds)


@pytest.mark.asyncio
async def test_readiness_wait_has_a_real_bounded_timeout():
    evaluator = CrossSessionEvaluator(
        _SlowReadinessRuntime(),
        _Judge(),
        profile=Profile.MOCK,
        backend="mock",
        readiness_timeout_seconds=0.01,
    )

    result = await evaluator.evaluate(_case())

    assert result.outcome is Outcome.DEPENDENCY_ERROR
    assert result.readiness is MemoryReadinessStatus.TIMEOUT
    assert result.reason_codes == ("memory_readiness_timeout",)
    assert result.no_ltm is None and result.with_ltm is None


def test_external_provider_and_invalid_timeout_are_rejected():
    with pytest.raises(ValueError, match="external provider"):
        CrossSessionEvaluator(
            _Runtime(),
            _Judge(),
            profile=Profile.EXTERNAL_SYNTHETIC,
            backend="mock",
        )
    with pytest.raises(ValueError, match="positive"):
        CrossSessionEvaluator(
            _Runtime(),
            _Judge(),
            profile=Profile.MOCK,
            backend="mock",
            readiness_timeout_seconds=0,
        )
