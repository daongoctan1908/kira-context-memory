"""Native rewrite evaluation with deterministic gates before semantic judgment."""

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import model_validator

from app.domain.errors.query_rewriter import (
    QueryRewriterConfigurationError,
    QueryRewriterConnectionError,
    QueryRewriterHttpError,
    QueryRewriterProtocolError,
    QueryRewriterTimeoutError,
)
from app.domain.models.context import ConversationContext
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory import LongTermMemory
from app.domain.ports.query_rewriter import QueryRewriterPort
from app.infrastructure.llm.vllm_query_rewriter import VllmQueryRewriterAdapter
from evaluation.judge import InternalSemanticJudge, JudgeError
from evaluation.measurement import measure_stage
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    EvalCase,
    EvalModel,
    Identifier,
    NonEmpty,
    Outcome,
    Profile,
    RewriteInput,
    Sha256,
    Suite,
)
from evaluation.scoring import (
    ConstraintScore,
    JudgeVerdict,
    SemanticJudgment,
    output_sha256,
    score_constraints,
)
from evaluation.timing import TimingStage


class RewriteSourceTimestampMissing(ValueError):
    """An eval fixture cannot supply a real source time for its recent context."""


class RewriteJudgePort(Protocol):
    async def semantic(
        self,
        *,
        case_id: str,
        suite: Literal[Suite.REWRITE, Suite.CROSS_SESSION],
        output: object,
        semantic_expectation: str,
        reference_answer: object | None = None,
        required_exact: Sequence[str] = (),
        forbidden: Sequence[str] = (),
    ) -> SemanticJudgment: ...


class RewriteCaseEvaluation(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    case_id: Identifier
    outcome: Outcome
    rewritten_query: NonEmpty | None = None
    output_sha256: Sha256 | None = None
    constraints: ConstraintScore | None = None
    judgment: SemanticJudgment | None = None
    reason_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def result_is_consistent(self) -> "RewriteCaseEvaluation":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("rewrite reason codes must be unique")
        if (self.rewritten_query is None) != (self.output_sha256 is None):
            raise ValueError("rewrite output and hash must be present together")
        if self.rewritten_query is not None:
            if self.output_sha256 != output_sha256(self.rewritten_query):
                raise ValueError("rewrite output hash does not match output")
        if self.judgment is not None:
            if self.judgment.case_id != self.case_id or self.judgment.suite is not Suite.REWRITE:
                raise ValueError("rewrite judgment is bound to another case or suite")
            if self.judgment.output_sha256 != self.output_sha256:
                raise ValueError("rewrite judgment is bound to another output")
            expected_outcome = {
                JudgeVerdict.PASS: Outcome.PASS,
                JudgeVerdict.FAIL: Outcome.FAIL,
                JudgeVerdict.UNCERTAIN: Outcome.REVIEW_REQUIRED,
            }[self.judgment.verdict]
            if self.outcome is not expected_outcome:
                raise ValueError("rewrite outcome does not match semantic judgment")
        if self.constraints is not None and not self.constraints.passed:
            if self.outcome is not Outcome.FAIL or self.judgment is not None:
                raise ValueError("deterministic rewrite failure cannot be overridden by a judge")
        return self


def build_rewrite_context(case: EvalCase) -> ConversationContext:
    """Translate the provider-neutral eval case into the native rewrite input."""

    if not isinstance(case.inputs, RewriteInput):
        raise ValueError("rewrite evaluator requires a rewrite case")
    if any(
        message.role == "user" and message.timestamp is None
        for message in case.inputs.recent_messages
    ):
        raise RewriteSourceTimestampMissing("recent user messages require source timestamps")
    # Assistant timestamps are never serialized as user evidence. Reuse a real
    # fixture time only to satisfy ConversationMessage's internal time contract.
    known_time = next(
        (
            message.timestamp
            for message in case.inputs.recent_messages
            if message.timestamp is not None
        ),
        None,
    )
    if case.inputs.recent_messages and known_time is None:
        raise RewriteSourceTimestampMissing("recent messages require a real fixture timestamp")
    recent = tuple(
        ConversationMessage(
            session_id=message.session_id or "eval-rewrite",
            turn_id=message.message_id,
            role=ConversationRole(message.role),
            content=message.content,
            timestamp=message.timestamp or known_time,
        )
        for message in case.inputs.recent_messages
    )
    memories = tuple(
        LongTermMemory(
            memory_id=memory.gold_id,
            content=memory.text,
            score=1.0,
            metadata={"user_id": memory.user_id, "gold_id": memory.gold_id},
        )
        for memory in case.inputs.long_term_memories
    )
    return ConversationContext(
        recent_messages=recent,
        current_query=case.inputs.current_query,
        estimated_recent_tokens=0,
        long_term_memories=memories,
    )


class RewriteEvaluator:
    """Evaluate native rewrite output without allowing a judge to bypass hard constraints."""

    def __init__(
        self,
        rewriter: QueryRewriterPort,
        judge: RewriteJudgePort,
        *,
        profile: Profile,
        backend: Literal["native", "mock"],
    ) -> None:
        if profile is Profile.EXTERNAL_SYNTHETIC:
            raise ValueError("canonical rewrite evaluation cannot use an external provider")
        if profile in {Profile.PC_OPENAI_ACCEPTANCE, Profile.INTERNAL_TEST}:
            if backend != "native" or not isinstance(rewriter, VllmQueryRewriterAdapter):
                raise ValueError("acceptance rewrite evaluation requires the native adapter")
            if not isinstance(judge, InternalSemanticJudge):
                raise ValueError("acceptance rewrite evaluation requires the approved judge")
        self._rewriter = rewriter
        self._judge = judge

    async def evaluate(self, case: EvalCase) -> RewriteCaseEvaluation:
        self._inputs(case)
        if case.eligibility.status == "blocked":
            return self._failure(case, Outcome.NOT_RUN, "rewrite_case_blocked")

        try:
            context = build_rewrite_context(case)
        except RewriteSourceTimestampMissing:
            return self._failure(case, Outcome.NOT_RUN, "rewrite_source_timestamp_missing")
        try:
            with measure_stage(TimingStage.REWRITE):
                rewritten = await self._rewriter.rewrite(context)
        except (
            QueryRewriterConfigurationError,
            QueryRewriterConnectionError,
            QueryRewriterHttpError,
            QueryRewriterTimeoutError,
        ):
            return self._failure(case, Outcome.DEPENDENCY_ERROR, "rewrite_unavailable")
        except QueryRewriterProtocolError:
            return self._failure(case, Outcome.PROTOCOL_ERROR, "rewrite_protocol_error")
        except Exception:
            return self._failure(case, Outcome.PROTOCOL_ERROR, "rewrite_unexpected_error")

        constraints = score_constraints(
            rewritten,
            required_exact=case.gold.required_exact,
            forbidden=case.gold.forbidden,
        )
        digest = output_sha256(rewritten)
        if not constraints.passed:
            reasons = tuple(
                reason
                for condition, reason in (
                    (constraints.missing_required, "rewrite_missing_required"),
                    (constraints.present_forbidden, "rewrite_present_forbidden"),
                )
                if condition
            )
            return RewriteCaseEvaluation(
                case_id=case.case_id,
                outcome=Outcome.FAIL,
                rewritten_query=rewritten,
                output_sha256=digest,
                constraints=constraints,
                reason_codes=reasons,
            )

        try:
            judgment = await self._judge.semantic(
                case_id=case.case_id,
                suite=Suite.REWRITE,
                output=rewritten,
                semantic_expectation=case.gold.semantic_expectation,
                reference_answer=case.gold.expected_rewrite,
                required_exact=case.gold.required_exact,
                forbidden=case.gold.forbidden,
            )
        except JudgeError as error:
            return RewriteCaseEvaluation(
                case_id=case.case_id,
                outcome=error.outcome,
                rewritten_query=rewritten,
                output_sha256=digest,
                constraints=constraints,
                reason_codes=(error.reason_code,),
            )
        except Exception:
            return RewriteCaseEvaluation(
                case_id=case.case_id,
                outcome=Outcome.PROTOCOL_ERROR,
                rewritten_query=rewritten,
                output_sha256=digest,
                constraints=constraints,
                reason_codes=("judge_unexpected_error",),
            )

        outcome = {
            JudgeVerdict.PASS: Outcome.PASS,
            JudgeVerdict.FAIL: Outcome.FAIL,
            JudgeVerdict.UNCERTAIN: Outcome.REVIEW_REQUIRED,
        }[judgment.verdict]
        return RewriteCaseEvaluation(
            case_id=case.case_id,
            outcome=outcome,
            rewritten_query=rewritten,
            output_sha256=digest,
            constraints=constraints,
            judgment=judgment,
            reason_codes=(judgment.reason_code,)
            if judgment.verdict is not JudgeVerdict.PASS
            else (),
        )

    @staticmethod
    def _inputs(case: EvalCase) -> RewriteInput:
        if not isinstance(case.inputs, RewriteInput):
            raise ValueError("rewrite evaluator requires a rewrite case")
        if case.gold.expected_rewrite is None:
            raise ValueError("rewrite case requires an expected rewrite")
        return case.inputs

    @staticmethod
    def _failure(case: EvalCase, outcome: Outcome, reason: str) -> RewriteCaseEvaluation:
        return RewriteCaseEvaluation(
            case_id=case.case_id,
            outcome=outcome,
            reason_codes=(reason,),
        )
