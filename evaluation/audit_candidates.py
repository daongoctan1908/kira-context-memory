"""Derive content-free, hash-bound human-audit candidates from one completed native run."""

from pathlib import Path

from evaluation.artifacts import ArtifactRunManifest, ArtifactStore, CaseAttemptArtifact
from evaluation.audit import AuditCandidate
from evaluation.compiler import compile_dataset
from evaluation.cross_session import CrossSessionCaseEvaluation
from evaluation.models import BenchmarkVariant, EvalCase, Outcome, Profile, Suite
from evaluation.native_executor import NativeFormationCaseEvaluation
from evaluation.rewrite import RewriteCaseEvaluation
from evaluation.scoring import JudgeVerdict

_AUDITABLE_PROFILES = {Profile.PC_OPENAI_ACCEPTANCE, Profile.INTERNAL_TEST}
_QUALITY_OUTCOMES = {Outcome.PASS, Outcome.FAIL, Outcome.REVIEW_REQUIRED}


def _bundle_id(case: EvalCase) -> str:
    values = tuple(tag.removeprefix("bundle:") for tag in case.tags if tag.startswith("bundle:"))
    if len(values) != 1:
        raise ValueError("semantic audit case must identify exactly one source bundle")
    return values[0]


def _semantic_candidates(
    case: EvalCase,
    attempt: CaseAttemptArtifact,
    variant: BenchmarkVariant,
) -> tuple[AuditCandidate, ...]:
    if attempt.output is None or attempt.output_sha256 is None:
        return ()
    if attempt.outcome not in _QUALITY_OUTCOMES:
        return ()

    verdict: JudgeVerdict
    reason_code: str
    deterministic: JudgeVerdict | None = None
    if case.suite is Suite.FORMATION:
        result = NativeFormationCaseEvaluation.model_validate(attempt.output)
        if not result.judge_decisions:
            return ()
        return tuple(
            AuditCandidate(
                case_id=case.case_id,
                subject=f"formation_prediction_{decision.prediction_index}",
                bundle_id=_bundle_id(case),
                suite=case.suite,
                variant=variant,
                output_sha256=attempt.output_sha256,
                judge_verdict={
                    "MATCH": JudgeVerdict.PASS,
                    "NO_MATCH": JudgeVerdict.FAIL,
                    "UNCERTAIN": JudgeVerdict.UNCERTAIN,
                }[decision.verdict.value],
                judge_reason_code=decision.reason_code,
                safety_passed=True,
            )
            for decision in result.judge_decisions
        )
    elif case.suite is Suite.REWRITE:
        result = RewriteCaseEvaluation.model_validate(attempt.output)
        if result.judgment is None:
            return ()
        verdict = result.judgment.verdict
        reason_code = result.judgment.reason_code
        if result.constraints is not None:
            deterministic = JudgeVerdict.PASS if result.constraints.passed else JudgeVerdict.FAIL
        return (
            AuditCandidate(
                case_id=case.case_id,
                subject="rewrite",
                bundle_id=_bundle_id(case),
                suite=case.suite,
                variant=variant,
                output_sha256=result.judgment.output_sha256,
                judge_verdict=verdict,
                judge_reason_code=reason_code,
                deterministic_verdict=deterministic,
                safety_passed=True,
            ),
        )
    elif case.suite is Suite.CROSS_SESSION:
        result = CrossSessionCaseEvaluation.model_validate(attempt.output)
        if result.with_ltm is None or result.no_ltm is None:
            return ()
        candidates: list[AuditCandidate] = []
        for condition, arm in (
            ("no_ltm", result.no_ltm),
            ("with_ltm", result.with_ltm),
        ):
            for output_kind, judgment in (
                ("rewrite", arm.rewrite_judgment),
                ("final", arm.final_judgment),
            ):
                if judgment is None:
                    continue
                candidates.append(
                    AuditCandidate(
                        case_id=case.case_id,
                        subject=f"{output_kind}_{condition}",
                        bundle_id=_bundle_id(case),
                        suite=case.suite,
                        variant=variant,
                        output_sha256=judgment.output_sha256,
                        judge_verdict=judgment.verdict,
                        judge_reason_code=judgment.reason_code,
                        deterministic_verdict=(
                            JudgeVerdict.PASS
                            if output_kind == "final"
                            and arm.task_success is not None
                            and arm.task_success.passed
                            else JudgeVerdict.FAIL
                            if output_kind == "final" and arm.task_success is not None
                            else JudgeVerdict.PASS
                            if output_kind == "rewrite" and arm.rewrite_constraints.passed
                            else JudgeVerdict.FAIL
                        ),
                        safety_passed=not arm.safety_violation_codes,
                    )
                )
        return tuple(candidates)
    else:
        return ()


def _semantic_candidate(
    case: EvalCase,
    attempt: CaseAttemptArtifact,
    variant: BenchmarkVariant,
) -> AuditCandidate | None:
    """Compatibility helper for tests/callers expecting at most the first semantic subject."""

    candidates = _semantic_candidates(case, attempt, variant)
    return candidates[0] if candidates else None


def build_audit_candidates(*, run_root: Path, dataset_root: Path) -> tuple[AuditCandidate, ...]:
    """Load one immutable run and emit only semantic evidence safe for human audit tooling."""

    manifest = ArtifactRunManifest.model_validate_json(
        (run_root / "manifest.json").read_text(encoding="utf-8")
    )
    if manifest.identity.profile not in _AUDITABLE_PROFILES:
        raise ValueError("semantic audit candidates require a real-model benchmark profile")
    store = ArtifactStore.resume(run_root, expected_identity=manifest.identity)
    compilation = compile_dataset(dataset_root, seed=manifest.identity.seed)
    ordered_cases = tuple(
        case
        for suite in manifest.identity.suites
        for case in compilation.cases
        if case.suite is suite
    )
    if (
        compilation.dataset_sha256 != manifest.identity.dataset_sha256
        or tuple(case.case_id for case in ordered_cases) != manifest.identity.selected_case_ids
    ):
        raise ValueError("audit dataset or compiled case order differs from the run")
    cases = {case.case_id: case for case in ordered_cases}
    latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
    candidates: list[AuditCandidate] = []
    for case_id in manifest.identity.selected_case_ids:
        attempt = latest.get(case_id)
        if attempt is None:
            continue
        candidates.extend(_semantic_candidates(cases[case_id], attempt, manifest.identity.variant))
    if not candidates:
        raise ValueError("run contains no completed semantic judgments to audit")
    return tuple(candidates)


__all__ = ["build_audit_candidates"]
