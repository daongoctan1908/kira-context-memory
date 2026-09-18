"""Paired cross-session evaluator for memory uplift and final-answer quality."""

import asyncio
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from evaluation.judge import InternalSemanticJudge, JudgeError
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    CrossSessionInput,
    EvalCase,
    EvalModel,
    Identifier,
    NonEmpty,
    Outcome,
    Profile,
    Suite,
)
from evaluation.rewrite import RewriteJudgePort
from evaluation.scoring import (
    ConstraintScore,
    JudgeVerdict,
    RetrievalScore,
    SemanticJudgment,
    TaskSuccessScore,
    output_sha256,
    score_constraints,
    score_retrieval_groups,
    score_task_success,
)


class CrossSessionCondition(StrEnum):
    NO_LTM = "no_ltm"
    WITH_LTM = "with_ltm"


class MemoryReadinessStatus(StrEnum):
    COMPLETED = "completed"
    DEAD = "dead"
    TIMEOUT = "timeout"


class MemoryJobHandle(EvalModel):
    event_id: UUID


class MemoryReadiness(EvalModel):
    event_id: UUID
    status: MemoryReadinessStatus
    gold_ids_by_memory_id: dict[NonEmpty, tuple[Identifier, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def terminal_state_is_consistent(self) -> "MemoryReadiness":
        if self.status is not MemoryReadinessStatus.COMPLETED and self.gold_ids_by_memory_id:
            raise ValueError("unfinished memory job cannot expose a formed corpus")
        return self


class RetrievedMemory(EvalModel):
    memory_id: NonEmpty
    user_id: Identifier
    score: float = Field(ge=0, le=1, allow_inf_nan=False)


class SessionBExecution(EvalModel):
    condition: CrossSessionCondition
    user_id: Identifier
    session_id: Identifier
    current_query: NonEmpty
    provider_id: NonEmpty
    retrieved_memories: tuple[RetrievedMemory, ...] = ()
    rewritten_query: NonEmpty
    final_answer: NonEmpty
    actual_action: Identifier | None = None
    actual_api: dict[str, object] | None = None
    query_memory_event_id: UUID | None = None


class CrossSessionRuntimePort(Protocol):
    async def persist_session_a(self, case: EvalCase) -> MemoryJobHandle: ...

    async def wait_for_memory(
        self,
        handle: MemoryJobHandle,
        *,
        timeout_seconds: float,
    ) -> MemoryReadiness: ...

    async def run_session_b(
        self,
        case: EvalCase,
        *,
        condition: CrossSessionCondition,
        enable_ltm: bool,
        schedule_memory: bool,
    ) -> SessionBExecution: ...


class CrossSessionDependencyError(Exception):
    """Sanitized failure of a required runtime dependency."""


class CrossSessionProtocolError(Exception):
    """Sanitized invalid response from the evaluation runtime."""


class CrossSessionArmEvaluation(EvalModel):
    case_id: Identifier
    condition: CrossSessionCondition
    outcome: Outcome
    rewritten_query: NonEmpty
    final_answer: NonEmpty
    retrieval: RetrievalScore | None = None
    rewrite_constraints: ConstraintScore
    rewrite_judgment: SemanticJudgment | None = None
    final_judgment: SemanticJudgment | None = None
    task_success: TaskSuccessScore | None = None
    safety_violation_codes: tuple[Identifier, ...] = ()
    reason_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def arm_is_consistent(self) -> "CrossSessionArmEvaluation":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("cross-session reason codes must be unique")
        if len(self.safety_violation_codes) != len(set(self.safety_violation_codes)):
            raise ValueError("cross-session safety codes must be unique")
        if self.safety_violation_codes and self.outcome is not Outcome.FAIL:
            raise ValueError("cross-session safety violations are hard failures")
        if not self.rewrite_constraints.passed and self.rewrite_judgment is not None:
            raise ValueError("rewrite judge cannot override deterministic constraints")
        for judgment, output in (
            (self.rewrite_judgment, self.rewritten_query),
            (self.final_judgment, self.final_answer),
        ):
            if judgment is None:
                continue
            if judgment.case_id != self.case_id or judgment.suite is not Suite.CROSS_SESSION:
                raise ValueError("cross-session judgment is bound to another case or suite")
            if judgment.output_sha256 != output_sha256(output):
                raise ValueError("cross-session judgment is bound to another output")
        return self


class CrossSessionCaseEvaluation(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    case_id: Identifier
    outcome: Outcome
    event_id: UUID | None = None
    readiness: MemoryReadinessStatus | None = None
    no_ltm: CrossSessionArmEvaluation | None = None
    with_ltm: CrossSessionArmEvaluation | None = None
    reason_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def paired_arms_are_consistent(self) -> "CrossSessionCaseEvaluation":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("cross-session case reason codes must be unique")
        if (self.no_ltm is None) != (self.with_ltm is None):
            raise ValueError("cross-session conclusions require a complete paired result")
        if self.no_ltm is not None:
            if self.no_ltm.case_id != self.case_id:
                raise ValueError("no-LTM arm is bound to another case")
            if self.no_ltm.condition is not CrossSessionCondition.NO_LTM:
                raise ValueError("no-LTM arm has the wrong condition")
            assert self.with_ltm is not None
            if self.with_ltm.case_id != self.case_id:
                raise ValueError("with-LTM arm is bound to another case")
            if self.with_ltm.condition is not CrossSessionCondition.WITH_LTM:
                raise ValueError("with-LTM arm has the wrong condition")
        return self


class CrossSessionEvaluator:
    """Orchestrate one bounded formation followed by a controlled paired QA run."""

    def __init__(
        self,
        runtime: CrossSessionRuntimePort,
        judge: RewriteJudgePort,
        *,
        profile: Profile,
        backend: Literal["native", "mock"],
        readiness_timeout_seconds: float = 30,
    ) -> None:
        if profile is Profile.EXTERNAL_SYNTHETIC:
            raise ValueError("canonical cross-session evaluation cannot use an external provider")
        if profile is Profile.INTERNAL_TEST:
            if backend != "native":
                raise ValueError("official cross-session evaluation requires the native runtime")
            if not isinstance(judge, InternalSemanticJudge):
                raise ValueError("official cross-session evaluation requires the internal judge")
        if readiness_timeout_seconds <= 0:
            raise ValueError("readiness timeout must be positive")
        self._runtime = runtime
        self._judge = judge
        self._readiness_timeout = float(readiness_timeout_seconds)

    async def evaluate(self, case: EvalCase) -> CrossSessionCaseEvaluation:
        inputs = self._inputs(case)
        if case.eligibility.status == "blocked":
            return self._failure(case, Outcome.NOT_RUN, "cross_session_case_blocked")

        handle: MemoryJobHandle | None = None
        try:
            handle = await self._runtime.persist_session_a(case)
            async with asyncio.timeout(self._readiness_timeout):
                readiness = await self._runtime.wait_for_memory(
                    handle,
                    timeout_seconds=self._readiness_timeout,
                )
        except TimeoutError:
            return self._failure(
                case,
                Outcome.DEPENDENCY_ERROR,
                "memory_readiness_timeout",
                event_id=handle.event_id if handle is not None else None,
                readiness=MemoryReadinessStatus.TIMEOUT,
            )
        except CrossSessionDependencyError:
            return self._failure(case, Outcome.DEPENDENCY_ERROR, "cross_session_dependency_error")
        except CrossSessionProtocolError:
            return self._failure(case, Outcome.PROTOCOL_ERROR, "cross_session_protocol_error")
        except Exception:
            return self._failure(case, Outcome.PROTOCOL_ERROR, "cross_session_unexpected_error")

        if readiness.event_id != handle.event_id:
            return self._failure(
                case,
                Outcome.PROTOCOL_ERROR,
                "memory_readiness_event_mismatch",
                event_id=handle.event_id,
            )
        if readiness.status is MemoryReadinessStatus.DEAD:
            return self._failure(
                case,
                Outcome.DEPENDENCY_ERROR,
                "memory_job_dead",
                event_id=handle.event_id,
                readiness=readiness.status,
            )
        if readiness.status is MemoryReadinessStatus.TIMEOUT:
            return self._failure(
                case,
                Outcome.DEPENDENCY_ERROR,
                "memory_readiness_timeout",
                event_id=handle.event_id,
                readiness=readiness.status,
            )

        try:
            no_ltm_execution = await self._runtime.run_session_b(
                case,
                condition=CrossSessionCondition.NO_LTM,
                enable_ltm=False,
                schedule_memory=False,
            )
            with_ltm_execution = await self._runtime.run_session_b(
                case,
                condition=CrossSessionCondition.WITH_LTM,
                enable_ltm=True,
                schedule_memory=False,
            )
        except CrossSessionDependencyError:
            return self._failure(
                case,
                Outcome.DEPENDENCY_ERROR,
                "session_b_dependency_error",
                event_id=handle.event_id,
                readiness=readiness.status,
            )
        except CrossSessionProtocolError:
            return self._failure(
                case,
                Outcome.PROTOCOL_ERROR,
                "session_b_protocol_error",
                event_id=handle.event_id,
                readiness=readiness.status,
            )
        except Exception:
            return self._failure(
                case,
                Outcome.PROTOCOL_ERROR,
                "session_b_unexpected_error",
                event_id=handle.event_id,
                readiness=readiness.status,
            )

        pair_violations = self._pair_violations(inputs, no_ltm_execution, with_ltm_execution)
        no_ltm = await self._evaluate_arm(
            case,
            no_ltm_execution,
            readiness,
            pair_violations,
        )
        with_ltm = await self._evaluate_arm(
            case,
            with_ltm_execution,
            readiness,
            pair_violations,
        )
        outcome = self._combined_outcome((no_ltm.outcome, with_ltm.outcome))
        return CrossSessionCaseEvaluation(
            case_id=case.case_id,
            outcome=outcome,
            event_id=handle.event_id,
            readiness=readiness.status,
            no_ltm=no_ltm,
            with_ltm=with_ltm,
        )

    async def _evaluate_arm(
        self,
        case: EvalCase,
        execution: SessionBExecution,
        readiness: MemoryReadiness,
        pair_violations: tuple[str, ...],
    ) -> CrossSessionArmEvaluation:
        constraints = score_constraints(
            execution.rewritten_query,
            required_exact=case.gold.required_exact,
            forbidden=case.gold.forbidden,
        )
        violations = list(pair_violations)
        inputs = self._inputs(case)
        if execution.user_id != inputs.user_id or execution.session_id != inputs.session_b:
            violations.append("cross_session_scope_mismatch")
        if execution.query_memory_event_id is not None:
            violations.append("session_b_corpus_contamination")
        if any(memory.user_id != inputs.user_id for memory in execution.retrieved_memories):
            violations.append("cross_session_user_leak")
        if execution.condition is CrossSessionCondition.NO_LTM and execution.retrieved_memories:
            violations.append("no_ltm_retrieval_not_bypassed")

        groups: list[tuple[str, ...]] = []
        foreign = False
        for memory in execution.retrieved_memories:
            mapped = readiness.gold_ids_by_memory_id.get(memory.memory_id)
            if mapped is None:
                foreign = True
                mapped = ()
            groups.append(mapped)
        if foreign:
            violations.append("cross_session_foreign_memory")
        retrieval = (
            score_retrieval_groups(case.gold.relevant_memory_ids, groups)
            if execution.condition is CrossSessionCondition.WITH_LTM
            else None
        )

        reasons: list[str] = []
        rewrite_judgment: SemanticJudgment | None = None
        final_judgment: SemanticJudgment | None = None
        error_outcomes: list[Outcome] = []
        if not constraints.passed:
            reasons.append("rewrite_constraint_failed")
        else:
            rewrite_judgment, error = await self._judge_output(
                case=case,
                output=execution.rewritten_query,
                semantic_expectation=(
                    "Rewrite the current query as a standalone query using only supported "
                    "context; preserve intent and constraints and do not answer the query."
                ),
                reference_answer={
                    "expected_rewrite": case.gold.expected_rewrite,
                    "current_query": inputs.session_b_query,
                },
                required_exact=case.gold.required_exact,
                forbidden=case.gold.forbidden,
            )
            if error is not None:
                error_outcomes.append(error[0])
                reasons.append(error[1])

        final_judgment, error = await self._judge_output(
            case=case,
            output=execution.final_answer,
            semantic_expectation=case.gold.semantic_expectation,
            reference_answer=case.gold.expected_answer,
        )
        if error is not None:
            error_outcomes.append(error[0])
            reasons.append(error[1])

        task = (
            score_task_success(
                expected_action=case.gold.expected_action,
                actual_action=execution.actual_action,
                expected_api=case.gold.expected_api,
                actual_api=execution.actual_api,
            )
            if case.gold.expected_action is not None and execution.actual_action is not None
            else None
        )
        if case.gold.expected_action is not None and execution.actual_action is None:
            reasons.append("task_outcome_unobservable")

        outcome = self._arm_outcome(
            violations=violations,
            constraints=constraints,
            retrieval=retrieval,
            rewrite_judgment=rewrite_judgment,
            final_judgment=final_judgment,
            task=task,
            error_outcomes=error_outcomes,
        )
        return CrossSessionArmEvaluation(
            case_id=case.case_id,
            condition=execution.condition,
            outcome=outcome,
            rewritten_query=execution.rewritten_query,
            final_answer=execution.final_answer,
            retrieval=retrieval,
            rewrite_constraints=constraints,
            rewrite_judgment=rewrite_judgment,
            final_judgment=final_judgment,
            task_success=task,
            safety_violation_codes=tuple(dict.fromkeys(violations)),
            reason_codes=tuple(dict.fromkeys(reasons)),
        )

    async def _judge_output(
        self,
        *,
        case: EvalCase,
        output: object,
        semantic_expectation: str,
        reference_answer: object | None,
        required_exact: Sequence[str] = (),
        forbidden: Sequence[str] = (),
    ) -> tuple[SemanticJudgment | None, tuple[Outcome, str] | None]:
        try:
            judgment = await self._judge.semantic(
                case_id=case.case_id,
                suite=Suite.CROSS_SESSION,
                output=output,
                semantic_expectation=semantic_expectation,
                reference_answer=reference_answer,
                required_exact=required_exact,
                forbidden=forbidden,
            )
            return judgment, None
        except JudgeError as error:
            return None, (error.outcome, error.reason_code)
        except Exception:
            return None, (Outcome.PROTOCOL_ERROR, "judge_unexpected_error")

    @staticmethod
    def _pair_violations(
        inputs: CrossSessionInput,
        no_ltm: SessionBExecution,
        with_ltm: SessionBExecution,
    ) -> tuple[str, ...]:
        violations: list[str] = []
        if (
            no_ltm.condition is not CrossSessionCondition.NO_LTM
            or with_ltm.condition is not CrossSessionCondition.WITH_LTM
        ):
            violations.append("cross_session_condition_mismatch")
        if (
            no_ltm.current_query != inputs.session_b_query
            or with_ltm.current_query != inputs.session_b_query
        ):
            violations.append("cross_session_query_mismatch")
        if no_ltm.current_query != with_ltm.current_query:
            violations.append("cross_session_pair_query_drift")
        if no_ltm.provider_id != with_ltm.provider_id:
            violations.append("cross_session_pair_provider_drift")
        return tuple(violations)

    @staticmethod
    def _arm_outcome(
        *,
        violations: Sequence[str],
        constraints: ConstraintScore,
        retrieval: RetrievalScore | None,
        rewrite_judgment: SemanticJudgment | None,
        final_judgment: SemanticJudgment | None,
        task: TaskSuccessScore | None,
        error_outcomes: Sequence[Outcome],
    ) -> Outcome:
        if violations or not constraints.passed:
            return Outcome.FAIL
        if Outcome.PROTOCOL_ERROR in error_outcomes:
            return Outcome.PROTOCOL_ERROR
        if Outcome.DEPENDENCY_ERROR in error_outcomes:
            return Outcome.DEPENDENCY_ERROR
        if retrieval is not None and retrieval.recall_at_3 not in (None, 1.0):
            return Outcome.FAIL
        if task is not None and not task.passed:
            return Outcome.FAIL
        judgments = tuple(item for item in (rewrite_judgment, final_judgment) if item is not None)
        if any(item.verdict is JudgeVerdict.FAIL for item in judgments):
            return Outcome.FAIL
        if any(item.verdict is JudgeVerdict.UNCERTAIN for item in judgments):
            return Outcome.REVIEW_REQUIRED
        return Outcome.PASS

    @staticmethod
    def _combined_outcome(outcomes: Sequence[Outcome]) -> Outcome:
        for outcome in (
            Outcome.PROTOCOL_ERROR,
            Outcome.DEPENDENCY_ERROR,
            Outcome.FAIL,
            Outcome.REVIEW_REQUIRED,
        ):
            if outcome in outcomes:
                return outcome
        return Outcome.PASS

    @staticmethod
    def _inputs(case: EvalCase) -> CrossSessionInput:
        if not isinstance(case.inputs, CrossSessionInput):
            raise ValueError("cross-session evaluator requires a cross-session case")
        return case.inputs

    @staticmethod
    def _failure(
        case: EvalCase,
        outcome: Outcome,
        reason: str,
        *,
        event_id: UUID | None = None,
        readiness: MemoryReadinessStatus | None = None,
    ) -> CrossSessionCaseEvaluation:
        return CrossSessionCaseEvaluation(
            case_id=case.case_id,
            outcome=outcome,
            event_id=event_id,
            readiness=readiness,
            reason_codes=(reason,),
        )
