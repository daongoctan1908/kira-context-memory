"""Minimal, deterministic benchmark scoring contracts.

Semantic equivalence is deliberately supplied as an explicit judgment.  This module never calls
an LLM, never reads telemetry, and never turns dependency failures into quality scores.
"""

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from evaluation.models import EvalModel, Identifier, NonEmpty, Profile, Sha256, Suite

_WHITESPACE = re.compile(r"\s+")
_MRR_DEPTH = 10


class JudgeVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


class FormationMatchVerdict(StrEnum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    UNCERTAIN = "UNCERTAIN"


class MatchSource(StrEnum):
    EXACT_NORMALIZED = "exact_normalized"
    INTERNAL_JUDGE = "internal_judge"


class JudgeProvenance(EvalModel):
    """Identity of an approved, explicitly configured semantic judge."""

    profile: Literal[Profile.PC_OPENAI_ACCEPTANCE, Profile.INTERNAL_TEST] = Profile.INTERNAL_TEST
    provider: Identifier
    model: NonEmpty
    deployment: NonEmpty | None = None
    prompt_sha256: Sha256
    response_schema_sha256: Sha256
    temperature: Literal[0] = 0


class SemanticJudgment(EvalModel):
    """Content-free semantic verdict bound to one immutable evaluated output."""

    case_id: Identifier
    suite: Literal[Suite.REWRITE, Suite.CROSS_SESSION]
    output_sha256: Sha256
    verdict: JudgeVerdict
    reason_code: Identifier
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    judge: JudgeProvenance


class FormationMatchDecision(EvalModel):
    prediction_index: int = Field(ge=0, strict=True)
    verdict: FormationMatchVerdict
    gold_id: Identifier | None = None
    reason_code: Identifier
    judge: JudgeProvenance

    @model_validator(mode="after")
    def match_requires_gold(self) -> "FormationMatchDecision":
        if (self.verdict is FormationMatchVerdict.MATCH) != (self.gold_id is not None):
            raise ValueError("only MATCH formation decisions may identify one gold fact")
        return self


class FormationMatch(EvalModel):
    prediction_index: int = Field(ge=0, strict=True)
    gold_id: Identifier
    source: MatchSource


class FormationScore(EvalModel):
    true_positive: int = Field(ge=0, strict=True)
    false_positive: int | None = Field(default=None, ge=0, strict=True)
    false_negative: int | None = Field(default=None, ge=0, strict=True)
    precision: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    recall: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    f1: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    matches: tuple[FormationMatch, ...] = ()
    needs_judge_prediction_indexes: tuple[int, ...] = ()
    uncertain_prediction_indexes: tuple[int, ...] = ()
    complete: bool


ScopeRaw = Literal["CONVERSATION", "GLOBAL", "MISSING", "INVALID"]


def classify_scope(raw: object) -> ScopeRaw:
    """Mirror vendored ``_enforce_memory_scopes`` without mutating any candidate.

    None/blank maps to MISSING (persist as CONVERSATION); case-insensitive enum
    values normalize; anything else is INVALID (dropped before embed/persist).
    """

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "MISSING"
    if not isinstance(raw, str):
        return "INVALID"
    normalized = raw.strip().upper()
    if normalized in ("CONVERSATION", "GLOBAL"):
        return normalized  # type: ignore[return-value]
    return "INVALID"


class ScopeSemanticsScore(EvalModel):
    false_global_promotion: bool = False
    persistence_miss: bool = False
    missed_global_count: int = Field(default=0, ge=0, strict=True)
    fallback_count: int = Field(default=0, ge=0, strict=True)
    invalid_count: int = Field(default=0, ge=0, strict=True)

    @property
    def violated(self) -> bool:
        return self.false_global_promotion or self.persistence_miss


class RetrievalScore(EvalModel):
    recall_at_3: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    reciprocal_rank: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    mrr_depth: Literal[10] = _MRR_DEPTH
    first_relevant_rank: int | None = Field(default=None, ge=1, le=_MRR_DEPTH, strict=True)


class ConstraintScore(EvalModel):
    passed: bool
    missing_required: tuple[str, ...] = ()
    present_forbidden: tuple[str, ...] = ()


class TaskSuccessScore(EvalModel):
    passed: bool
    action_matches: bool
    api_matches: bool | None = None
    mismatch_paths: tuple[str, ...] = ()


class SafetyScore(EvalModel):
    passed: bool
    violation_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def state_is_consistent(self) -> "SafetyScore":
        if self.passed == bool(self.violation_codes):
            raise ValueError("safety pass state must be the inverse of violations")
        if len(self.violation_codes) != len(set(self.violation_codes)):
            raise ValueError("safety violation codes must be unique")
        return self


def output_sha256(output: object) -> str:
    """Hash a stable JSON representation so reviews cannot float across changed outputs."""

    encoded = json.dumps(
        output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def normalize_exact(value: str) -> str:
    """Normalize Unicode/case/whitespace only; never erase operators, units or punctuation."""

    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value).strip()).casefold()


def score_formation(
    gold_facts: Mapping[str, str],
    predicted_facts: Sequence[str],
    *,
    judge_decisions: Sequence[FormationMatchDecision] = (),
) -> FormationScore:
    """One-to-one exact-first fact matching, with explicit decisions for semantic leftovers."""

    if len(gold_facts) != len(set(gold_facts)):
        raise ValueError("formation gold IDs must be unique")

    unmatched_gold = set(gold_facts)
    unmatched_predictions = set(range(len(predicted_facts)))
    matches: list[FormationMatch] = []
    normalized_gold: dict[str, list[str]] = {}
    for gold_id, text in gold_facts.items():
        normalized_gold.setdefault(normalize_exact(text), []).append(gold_id)

    for prediction_index, text in enumerate(predicted_facts):
        candidates = normalized_gold.get(normalize_exact(text), [])
        gold_id = next((candidate for candidate in candidates if candidate in unmatched_gold), None)
        if gold_id is None:
            continue
        unmatched_gold.remove(gold_id)
        unmatched_predictions.remove(prediction_index)
        matches.append(
            FormationMatch(
                prediction_index=prediction_index,
                gold_id=gold_id,
                source=MatchSource.EXACT_NORMALIZED,
            )
        )

    decisions_by_prediction: dict[int, FormationMatchDecision] = {}
    uncertain: list[int] = []
    for decision in judge_decisions:
        index = decision.prediction_index
        if index in decisions_by_prediction:
            raise ValueError("formation prediction has multiple judge decisions")
        if index < 0 or index >= len(predicted_facts):
            raise ValueError("formation judge decision references an unknown prediction")
        if index not in unmatched_predictions:
            raise ValueError("formation judge cannot override an exact match")
        decisions_by_prediction[index] = decision
        if decision.verdict is FormationMatchVerdict.MATCH:
            assert decision.gold_id is not None
            if decision.gold_id not in unmatched_gold:
                raise ValueError("formation judge match must reference one unmatched gold fact")
            unmatched_gold.remove(decision.gold_id)
            unmatched_predictions.remove(index)
            matches.append(
                FormationMatch(
                    prediction_index=index,
                    gold_id=decision.gold_id,
                    source=MatchSource.INTERNAL_JUDGE,
                )
            )
        elif decision.verdict is FormationMatchVerdict.UNCERTAIN:
            uncertain.append(index)

    # A semantic decision is useful only while both sides have candidates. If no gold remains,
    # leftover predictions are deterministically false positives; if no prediction remains,
    # leftover gold is deterministically false negative.
    needs_judge = (
        sorted(index for index in unmatched_predictions if index not in decisions_by_prediction)
        if unmatched_gold
        else []
    )
    complete = not needs_judge and not uncertain
    true_positive = len(matches)
    if not complete:
        return FormationScore(
            true_positive=true_positive,
            matches=tuple(sorted(matches, key=lambda match: match.prediction_index)),
            needs_judge_prediction_indexes=tuple(needs_judge),
            uncertain_prediction_indexes=tuple(sorted(uncertain)),
            complete=False,
        )

    false_positive = len(unmatched_predictions)
    false_negative = len(unmatched_gold)
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    precision = true_positive / precision_denominator if precision_denominator else None
    recall = true_positive / recall_denominator if recall_denominator else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else (0.0 if precision is not None and recall is not None else None)
    )
    return FormationScore(
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        precision=precision,
        recall=recall,
        f1=f1,
        matches=tuple(sorted(matches, key=lambda match: match.prediction_index)),
        complete=True,
    )


def score_retrieval(
    relevant_memory_ids: Sequence[str],
    returned_memory_ids: Sequence[str],
) -> RetrievalScore:
    """Score only Recall@3 and MRR@10; no-hit cases have an explicit N/A denominator."""

    return score_retrieval_groups(
        relevant_memory_ids,
        tuple((memory_id,) for memory_id in returned_memory_ids),
    )


def score_retrieval_groups(
    relevant_memory_ids: Sequence[str],
    returned_gold_id_groups: Sequence[Sequence[str]],
) -> RetrievalScore:
    """Score ranked rows that may each represent more than one formation gold fact."""

    relevant = set(relevant_memory_ids)
    if not relevant:
        return RetrievalScore()
    top_three = {gold_id for group in returned_gold_id_groups[:3] for gold_id in group}
    recall = len(relevant.intersection(top_three)) / len(relevant)
    first_rank = next(
        (
            rank
            for rank, gold_ids in enumerate(returned_gold_id_groups[:_MRR_DEPTH], 1)
            if relevant.intersection(gold_ids)
        ),
        None,
    )
    return RetrievalScore(
        recall_at_3=recall,
        reciprocal_rank=(1 / first_rank if first_rank is not None else 0.0),
        first_relevant_rank=first_rank,
    )


def score_constraints(
    output: str,
    *,
    required_exact: Sequence[str] = (),
    forbidden: Sequence[str] = (),
) -> ConstraintScore:
    normalized_output = normalize_exact(output)
    missing = tuple(
        value for value in required_exact if normalize_exact(value) not in normalized_output
    )
    present = tuple(value for value in forbidden if normalize_exact(value) in normalized_output)
    return ConstraintScore(
        passed=not missing and not present,
        missing_required=missing,
        present_forbidden=present,
    )


def _compare_expected(
    expected: object,
    actual: object,
    *,
    path: str,
    mismatches: list[str],
) -> None:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            mismatches.append(path)
            return
        for key, value in expected.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in actual:
                mismatches.append(child)
            else:
                _compare_expected(value, actual[key], path=child, mismatches=mismatches)
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or expected != actual:
            mismatches.append(path)
        return
    if expected != actual:
        mismatches.append(path)


def score_task_success(
    *,
    expected_action: str,
    actual_action: str,
    expected_api: Mapping[str, Any] | None = None,
    actual_api: Mapping[str, Any] | None = None,
) -> TaskSuccessScore:
    action_matches = expected_action == actual_action
    mismatches: list[str] = []
    api_matches: bool | None = None
    if expected_api is not None:
        if actual_api is None:
            mismatches.append("api")
        else:
            _compare_expected(expected_api, actual_api, path="api", mismatches=mismatches)
        api_matches = not mismatches
    return TaskSuccessScore(
        passed=action_matches and (api_matches is not False),
        action_matches=action_matches,
        api_matches=api_matches,
        mismatch_paths=tuple(mismatches),
    )


def score_safety(violation_codes: Sequence[str]) -> SafetyScore:
    unique = tuple(dict.fromkeys(violation_codes))
    return SafetyScore(passed=not unique, violation_codes=unique)


def score_scope_semantics(
    *,
    negative: bool,
    gold_scope: Literal["CONVERSATION", "GLOBAL"] | None = None,
    matched_pairs: Sequence[tuple[ScopeRaw, bool]] = (),
    negative_predictions: Sequence[ScopeRaw] = (),
) -> ScopeSemanticsScore:
    """Scope layer over text-matched formation pairs (docs/evaluator-scope-semantics.md).

    matched_pairs: one entry per (gold, predicted) text match — predicted scope
    classified from the raw LLM field (or the persisted payload when a row
    exists), and whether the pair has a corresponding persisted ADD. Negative
    cases never match gold, so their scope diagnostics ride on
    negative_predictions; the false-ADD gate itself stays in ``score_formation``
    and counts every predicted fact regardless of scope.
    """

    if negative:
        return ScopeSemanticsScore(
            # Repurposed per spec: on a negative turn this is the diagnostic
            # negative_global_prediction_count.
            missed_global_count=sum(1 for scope in negative_predictions if scope == "GLOBAL"),
            fallback_count=sum(1 for scope in negative_predictions if scope == "MISSING"),
            invalid_count=sum(1 for scope in negative_predictions if scope == "INVALID"),
        )
    missed_global = 0
    fallback = 0
    invalid = 0
    persistence_miss = False
    promotion = False
    for predicted, persisted in matched_pairs:
        if predicted == "MISSING":
            fallback += 1
        if predicted == "INVALID":
            invalid += 1
        if not persisted:
            persistence_miss = True
        if gold_scope == "CONVERSATION" and predicted == "GLOBAL":
            promotion = True
        if gold_scope == "GLOBAL" and predicted in ("CONVERSATION", "MISSING"):
            missed_global += 1
    return ScopeSemanticsScore(
        false_global_promotion=promotion,
        persistence_miss=persistence_miss,
        missed_global_count=missed_global,
        fallback_count=fallback,
        invalid_count=invalid,
    )


def mean_metric(values: Sequence[float | None]) -> float | None:
    """Aggregate scored cases without converting N/A/error cases to zero."""

    scored = [value for value in values if value is not None]
    if not scored:
        return None
    result = sum(scored) / len(scored)
    if not math.isfinite(result):
        raise ValueError("metric values must be finite")
    return result
