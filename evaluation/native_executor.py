"""Native suite orchestration without provider or deployment composition.

The executor in this module owns only benchmark semantics: suite dispatch, formation scoring and
the two retrieval views.  Concrete PostgreSQL, Mem0, KiRa and HTTP clients are injected by the
runtime composition root, which keeps this layer deterministic and prevents an evaluation-only
transport from leaking into the production Gateway.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import model_validator

from evaluation.cross_session import CrossSessionCaseEvaluation
from evaluation.formation import (
    FormationExtractionResult,
    PersistentFormationResult,
)
from evaluation.judge import InternalSemanticJudge, JudgeError
from evaluation.models import EvalCase, EvalModel, FormationInput, Identifier, Outcome, Suite
from evaluation.retrieval import RetrievalCaseEvaluation
from evaluation.runner import BenchmarkExecutionResult
from evaluation.scoring import (
    FormationMatchDecision,
    FormationScore,
    ScopeSemanticsScore,
    classify_scope,
    score_formation,
    score_scope_semantics,
)


class NativeCaseEvaluator(Protocol):
    async def evaluate(self, case: EvalCase) -> EvalModel: ...


class NativeFormationRuntime(Protocol):
    async def evaluate(
        self, case: EvalCase
    ) -> FormationExtractionResult | PersistentFormationResult: ...


class NativeRetrievalRuntime(Protocol):
    async def evaluate_gold(self, case: EvalCase) -> RetrievalCaseEvaluation: ...

    async def evaluate_formed(self, case: EvalCase) -> RetrievalCaseEvaluation: ...


class NativeFormationCaseEvaluation(EvalModel):
    case_id: Identifier
    outcome: Outcome
    extraction: FormationExtractionResult | None = None
    persistence: PersistentFormationResult | None = None
    score: FormationScore | None = None
    scope_semantics: ScopeSemanticsScore | None = None
    judge_decisions: tuple[FormationMatchDecision, ...] = ()
    reason_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def state_is_consistent(self) -> "NativeFormationCaseEvaluation":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("formation reason codes must be unique")
        if self.outcome in {Outcome.PASS, Outcome.FAIL, Outcome.REVIEW_REQUIRED}:
            if self.extraction is None or self.score is None:
                raise ValueError("formation quality outcome requires extraction and score")
        elif self.score is not None or self.judge_decisions:
            raise ValueError("formation execution failure cannot contain a quality score")
        return self


class NativeRetrievalCaseEvaluation(EvalModel):
    case_id: Identifier
    outcome: Outcome
    gold_fixture: RetrievalCaseEvaluation | None = None
    formation_produced: RetrievalCaseEvaluation | None = None
    reason_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def modes_are_consistent(self) -> "NativeRetrievalCaseEvaluation":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("retrieval reason codes must be unique")
        if self.outcome in {Outcome.PASS, Outcome.FAIL}:
            if self.gold_fixture is None or self.formation_produced is None:
                raise ValueError("retrieval quality outcome requires both corpus modes")
        return self


class NativeFormationEvaluator:
    """Score one native formation, calling the judge only for semantic leftovers."""

    def __init__(self, runtime: NativeFormationRuntime, judge: InternalSemanticJudge) -> None:
        self._runtime = runtime
        self._judge = judge

    async def evaluate(self, case: EvalCase) -> NativeFormationCaseEvaluation:
        native = await self._runtime.evaluate(case)
        persistence = native if isinstance(native, PersistentFormationResult) else None
        extraction = native.extraction if persistence is not None else native
        if extraction is None or native.outcome not in {
            Outcome.PASS,
            Outcome.FAIL,
            Outcome.REVIEW_REQUIRED,
        }:
            return NativeFormationCaseEvaluation(
                case_id=case.case_id,
                outcome=native.outcome,
                extraction=extraction,
                persistence=persistence,
                reason_codes=tuple(native.reason_codes),
            )

        predicted = tuple(fact.text for fact in extraction.facts)
        gold = {fact.gold_id: fact.text for fact in case.gold.facts}
        contract = case.gold.formation_contract
        score = score_formation(gold, predicted, contract=contract)
        decisions: tuple[FormationMatchDecision, ...] = ()
        if score.needs_judge_prediction_indexes:
            try:
                assert isinstance(case.inputs, FormationInput)
                source_ids = set(case.inputs.source_message_ids)
                source = tuple(
                    message
                    for message in case.inputs.messages
                    if not source_ids or message.message_id in source_ids
                )
                context = tuple(
                    message
                    for message in case.inputs.messages
                    if source_ids and message.message_id not in source_ids
                )[-10:]
                matched_gold = {match.gold_id for match in score.matches}
                decisions = await self._judge.formation(
                    predicted_facts=predicted,
                    gold_facts={key: text for key, text in gold.items() if key not in matched_gold},
                    prediction_indexes=score.needs_judge_prediction_indexes,
                    contract=contract,
                    source_messages=tuple(message.model_dump(mode="json") for message in source),
                    context_messages=tuple(message.model_dump(mode="json") for message in context),
                    prediction_details=tuple(
                        {
                            "attributed_to": fact.attributed_to,
                            "memory_scope": (
                                "CONVERSATION"
                                if classify_scope(fact.scope) == "MISSING"
                                else classify_scope(fact.scope)
                            ),
                        }
                        for fact in extraction.facts
                    ),
                    gold_details={
                        fact.gold_id: {
                            "attributed_to": fact.attributed_to,
                            "evidence_message_ids": fact.evidence_message_ids,
                            "memory_scope": (
                                case.gold.lifecycle_event.memory_scope
                                if case.gold.lifecycle_event is not None
                                else None
                            ),
                        }
                        for fact in case.gold.facts
                    },
                    semantic_expectation=case.gold.semantic_expectation,
                    forbidden_facts=case.gold.forbidden_facts,
                )
                score = score_formation(
                    gold, predicted, judge_decisions=decisions, contract=contract
                )
            except JudgeError as error:
                return NativeFormationCaseEvaluation(
                    case_id=case.case_id,
                    outcome=error.outcome,
                    extraction=extraction,
                    persistence=persistence,
                    reason_codes=(error.reason_code,),
                )
            except Exception:
                return NativeFormationCaseEvaluation(
                    case_id=case.case_id,
                    outcome=Outcome.PROTOCOL_ERROR,
                    extraction=extraction,
                    persistence=persistence,
                    reason_codes=("judge_unexpected_error",),
                )

        if not score.complete:
            outcome = Outcome.REVIEW_REQUIRED
            reasons = ("formation_semantic_uncertain",)
        elif (score.false_positive or 0) or (score.false_negative or 0):
            outcome = Outcome.FAIL
            reasons = ("formation_quality_mismatch",)
        else:
            outcome = Outcome.PASS
            reasons = ()

        scope_semantics, scope_reasons = self._scope_layer(case, extraction, persistence, score)
        reasons = tuple(dict.fromkeys((*reasons, *scope_reasons)))
        if scope_reasons and outcome is Outcome.PASS:
            outcome = Outcome.FAIL
        return NativeFormationCaseEvaluation(
            case_id=case.case_id,
            outcome=outcome,
            extraction=extraction,
            persistence=persistence,
            score=score,
            scope_semantics=scope_semantics,
            judge_decisions=decisions,
            reason_codes=reasons,
        )

    @staticmethod
    def _scope_layer(
        case: EvalCase,
        extraction: FormationExtractionResult,
        persistence: PersistentFormationResult | None,
        score: FormationScore,
    ) -> tuple[ScopeSemanticsScore, tuple[Identifier, ...]]:
        """Scope gates over text-matched pairs (docs/evaluator-scope-semantics.md)."""

        gold = case.gold.lifecycle_event
        if gold is None or not gold.should_store:
            # Negative or un-annotated gold has no scope target. In open-world cases, valid
            # assertions outside the negative target may still be formed; text scoring decides.
            valid_extras = set(score.valid_extra_prediction_indexes)
            negative_predictions = tuple(
                classify_scope(fact.scope)
                for index, fact in enumerate(extraction.facts)
                if index not in valid_extras
            )
            return (
                score_scope_semantics(negative=True, negative_predictions=negative_predictions),
                (),
            )

        # On the persistent path the payload scope is the persisted truth (the
        # raw LLM field was normalized/dropped before writing); on the
        # write-free path reconciliation binds the receipt to lifecycle events,
        # so a matched text with no ADD means the memory was never persisted.
        payload_scopes: dict[str, str] = {}
        if persistence is not None and persistence.persistence is not None:
            payload_scopes = {
                memory.content: memory.memory_scope
                for memory in persistence.persistence.memories
                if memory.memory_scope is not None
            }
        lifecycle_texts = {event.memory for event in extraction.lifecycle_events}
        matched_pairs = []
        for match in score.matches:
            fact = extraction.facts[match.prediction_index]
            raw = payload_scopes.get(fact.text, fact.scope)
            matched_pairs.append((classify_scope(raw), fact.text in lifecycle_texts))
        semantics = score_scope_semantics(
            negative=False,
            gold_scope=gold.memory_scope,
            matched_pairs=matched_pairs,
        )
        reasons: list[Identifier] = []
        if semantics.false_global_promotion:
            reasons.append("scope_false_global_promotion")
        if semantics.persistence_miss:
            reasons.append("formation_persistence_miss")
        return semantics, tuple(reasons)


class NativeRetrievalEvaluator:
    """Keep gold-fixture and formation-produced retrieval evidence in one case artifact."""

    def __init__(self, runtime: NativeRetrievalRuntime) -> None:
        self._runtime = runtime

    async def evaluate(self, case: EvalCase) -> NativeRetrievalCaseEvaluation:
        gold = await self._runtime.evaluate_gold(case)
        formed = await self._runtime.evaluate_formed(case)
        outcome = _combined_outcome((gold.outcome, formed.outcome))
        reasons = tuple(dict.fromkeys((*gold.reason_codes, *formed.reason_codes)))
        return NativeRetrievalCaseEvaluation(
            case_id=case.case_id,
            outcome=outcome,
            gold_fixture=gold,
            formation_produced=formed,
            reason_codes=reasons,
        )


class NativeBenchmarkExecutor:
    """Dispatch a compiled case to an explicitly provided native suite evaluator."""

    def __init__(self, evaluators: Mapping[Suite, NativeCaseEvaluator]) -> None:
        if not evaluators:
            raise ValueError("native executor requires at least one suite evaluator")
        self._evaluators = dict(evaluators)

    async def evaluate(self, case: EvalCase) -> EvalModel:
        if case.eligibility.status == "blocked":
            return BenchmarkExecutionResult(
                case_id=case.case_id,
                outcome=Outcome.NOT_RUN,
                reason_codes=case.eligibility.blocked_reasons,
            )
        evaluator = self._evaluators.get(case.suite)
        if evaluator is None:
            return BenchmarkExecutionResult(
                case_id=case.case_id,
                outcome=Outcome.PROTOCOL_ERROR,
                reason_codes=("native_suite_not_configured",),
            )
        result = await evaluator.evaluate(case)
        if result.case_id != case.case_id:
            return BenchmarkExecutionResult(
                case_id=case.case_id,
                outcome=Outcome.PROTOCOL_ERROR,
                reason_codes=("native_case_identity_mismatch",),
            )
        return result


def _combined_outcome(outcomes: Sequence[Outcome]) -> Outcome:
    for outcome in (
        Outcome.PROTOCOL_ERROR,
        Outcome.DEPENDENCY_ERROR,
        Outcome.NOT_RUN,
        Outcome.FAIL,
        Outcome.REVIEW_REQUIRED,
        Outcome.INSUFFICIENT_EVIDENCE,
    ):
        if outcome in outcomes:
            return outcome
    return Outcome.PASS


__all__ = [
    "CrossSessionCaseEvaluation",
    "NativeBenchmarkExecutor",
    "NativeFormationCaseEvaluation",
    "NativeFormationEvaluator",
    "NativeRetrievalCaseEvaluation",
    "NativeRetrievalEvaluator",
]
