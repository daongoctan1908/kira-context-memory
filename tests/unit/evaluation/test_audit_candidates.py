"""Native semantic outputs become content-free, hash-bound audit candidates."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from evaluation.artifacts import ArtifactRunIdentity, ArtifactStore, CaseAttemptArtifact
from evaluation.audit_candidates import (
    _semantic_candidate,
    _semantic_candidates,
    build_audit_candidates,
)
from evaluation.compiler import compile_dataset
from evaluation.cross_session import (
    CrossSessionArmEvaluation,
    CrossSessionCaseEvaluation,
    CrossSessionCondition,
)
from evaluation.dataset import default_dataset_root
from evaluation.formation import (
    ExtractedFact,
    FormationExecutionStatus,
    FormationExtractionResult,
)
from evaluation.models import (
    BenchmarkVariant,
    CandidateChangeScope,
    CandidateDeclaration,
    GitSource,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.native_executor import NativeFormationCaseEvaluation
from evaluation.rewrite import RewriteCaseEvaluation
from evaluation.scoring import (
    ConstraintScore,
    FormationMatchDecision,
    FormationMatchVerdict,
    JudgeProvenance,
    JudgeVerdict,
    SemanticJudgment,
    TaskSuccessScore,
    output_sha256,
    score_formation,
)


def _judge() -> JudgeProvenance:
    return JudgeProvenance(
        provider="internal-judge",
        model="judge-model",
        prompt_sha256="a" * 64,
        response_schema_sha256="b" * 64,
    )


def _semantic(case_id: str, output: str, verdict: JudgeVerdict) -> SemanticJudgment:
    return SemanticJudgment(
        case_id=case_id,
        suite=Suite.REWRITE,
        output_sha256=output_sha256(output),
        verdict=verdict,
        reason_code="semantic_verdict",
        rationale="Content-free test rationale.",
        judge=_judge(),
    )


def _attempt(case, result) -> CaseAttemptArtifact:
    output = result.model_dump(mode="json", exclude_none=False)
    return CaseAttemptArtifact(
        case_id=case.case_id,
        suite=case.suite,
        attempt=1,
        completed_at=datetime(2026, 9, 19, tzinfo=UTC),
        outcome=result.outcome,
        output=output,
        output_sha256=output_sha256(output),
        reason_codes=tuple(result.reason_codes),
    )


def _cases():
    compilation = compile_dataset(default_dataset_root(), seed=742)
    return compilation, {
        suite: next(case for case in compilation.cases if case.suite is suite) for suite in Suite
    }


def test_formation_rewrite_and_final_qa_candidates_are_derived_without_content():
    _, cases = _cases()
    variant = BenchmarkVariant.RELEASE_CANDIDATE

    formation_case = cases[Suite.FORMATION]
    gold_id = formation_case.gold.facts[0].gold_id
    decision = FormationMatchDecision(
        prediction_index=0,
        verdict=FormationMatchVerdict.MATCH,
        gold_id=gold_id,
        reason_code="semantic_match",
        judge=_judge(),
    )
    extraction = FormationExtractionResult(
        case_id=formation_case.case_id,
        outcome=Outcome.REVIEW_REQUIRED,
        status=FormationExecutionStatus.VALID_FACTS,
        facts=(ExtractedFact(text="semantic paraphrase", attributed_to="user"),),
        provider_calls=1,
        stages=(),
    )
    formation = NativeFormationCaseEvaluation(
        case_id=formation_case.case_id,
        outcome=Outcome.PASS,
        extraction=extraction,
        score=score_formation(
            {gold_id: formation_case.gold.facts[0].text},
            ("semantic paraphrase",),
            judge_decisions=(decision,),
        ),
        judge_decisions=(decision,),
    )
    formed = _semantic_candidate(formation_case, _attempt(formation_case, formation), variant)
    assert formed is not None and formed.judge_verdict is JudgeVerdict.PASS
    assert formed.subject == "formation_prediction_0"
    assert formed.deterministic_verdict is None

    rewrite_case = cases[Suite.REWRITE]
    rewritten = "standalone rewritten query"
    rewrite_judgment = _semantic(rewrite_case.case_id, rewritten, JudgeVerdict.PASS)
    rewrite = RewriteCaseEvaluation(
        case_id=rewrite_case.case_id,
        outcome=Outcome.PASS,
        rewritten_query=rewritten,
        output_sha256=output_sha256(rewritten),
        constraints=ConstraintScore(passed=True),
        judgment=rewrite_judgment,
    )
    rewritten_candidate = _semantic_candidate(
        rewrite_case, _attempt(rewrite_case, rewrite), variant
    )
    assert rewritten_candidate is not None
    assert rewritten_candidate.deterministic_verdict is JudgeVerdict.PASS

    cross_case = cases[Suite.CROSS_SESSION]
    answer = "final answer"
    final_judgment = _semantic(cross_case.case_id, answer, JudgeVerdict.UNCERTAIN).model_copy(
        update={"suite": Suite.CROSS_SESSION}
    )

    def arm(condition: CrossSessionCondition, *, judgment=None, rewrite_judgment=None):
        return CrossSessionArmEvaluation(
            case_id=cross_case.case_id,
            condition=condition,
            outcome=Outcome.REVIEW_REQUIRED if judgment else Outcome.PASS,
            rewritten_query="rewritten",
            final_answer=answer,
            rewrite_constraints=ConstraintScore(passed=True),
            rewrite_judgment=rewrite_judgment,
            final_judgment=judgment,
            task_success=TaskSuccessScore(passed=False, action_matches=False),
        )

    cross = CrossSessionCaseEvaluation(
        case_id=cross_case.case_id,
        outcome=Outcome.REVIEW_REQUIRED,
        event_id=UUID(int=1),
        no_ltm=arm(CrossSessionCondition.NO_LTM),
        with_ltm=arm(CrossSessionCondition.WITH_LTM, judgment=final_judgment),
    )
    final = _semantic_candidate(cross_case, _attempt(cross_case, cross), variant)
    assert final is not None and final.judge_verdict is JudgeVerdict.UNCERTAIN
    assert final.deterministic_verdict is JudgeVerdict.FAIL
    assert final.safety_passed
    assert "final answer" not in final.model_dump_json()

    rewrite_judgment = _semantic(cross_case.case_id, "rewritten", JudgeVerdict.PASS).model_copy(
        update={"suite": Suite.CROSS_SESSION}
    )
    both_arms = cross.model_copy(
        update={
            "no_ltm": arm(
                CrossSessionCondition.NO_LTM,
                judgment=final_judgment.model_copy(update={"verdict": JudgeVerdict.PASS}),
                rewrite_judgment=rewrite_judgment,
            ),
            "with_ltm": arm(
                CrossSessionCondition.WITH_LTM,
                judgment=final_judgment,
                rewrite_judgment=rewrite_judgment,
            ),
        }
    )
    final_candidates = _semantic_candidates(cross_case, _attempt(cross_case, both_arms), variant)
    assert {candidate.subject for candidate in final_candidates} == {
        "final_no_ltm",
        "final_with_ltm",
        "rewrite_no_ltm",
        "rewrite_with_ltm",
    }


def _provenance() -> RunProvenance:
    source = GitSource(sha="1" * 40, dirty=False)
    return RunProvenance(
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": "2.0.20+viettel.4",
        },
        candidate=CandidateDeclaration(
            candidate_id="candidate-a",
            change_scopes=(CandidateChangeScope.PROMPT,),
            summary="Audit extraction test candidate.",
        ),
    )


def _store(tmp_path: Path, profile: Profile) -> tuple[ArtifactStore, object]:
    compilation = compile_dataset(default_dataset_root(), seed=742)
    ordered_cases = tuple(
        case for suite in Suite for case in compilation.cases if case.suite is suite
    )
    identity = ArtifactRunIdentity(
        run_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        profile=profile,
        variant=BenchmarkVariant.RELEASE_CANDIDATE,
        provenance=_provenance(),
        dataset_id=compilation.dataset_id,
        dataset_version=compilation.dataset_version,
        dataset_sha256=compilation.dataset_sha256,
        compilation_sha256="c" * 64,
        config_sha256="d" * 64,
        seed=742,
        suites=tuple(Suite),
        selected_case_ids=tuple(case.case_id for case in ordered_cases),
    )
    return (
        ArtifactStore.create(
            tmp_path / profile.value,
            identity=identity,
            created_at=datetime(2026, 9, 19, tzinfo=UTC),
        ),
        compilation,
    )


def test_build_candidates_binds_run_dataset_and_rejects_mock_or_empty(tmp_path: Path):
    mock, _ = _store(tmp_path, Profile.MOCK)
    with pytest.raises(ValueError, match="real-model"):
        build_audit_candidates(run_root=mock.root, dataset_root=default_dataset_root())

    live, compilation = _store(tmp_path, Profile.INTERNAL_TEST)
    rewrite_case = next(case for case in compilation.cases if case.suite is Suite.REWRITE)
    rewritten = "standalone rewritten query"
    result = RewriteCaseEvaluation(
        case_id=rewrite_case.case_id,
        outcome=Outcome.PASS,
        rewritten_query=rewritten,
        output_sha256=output_sha256(rewritten),
        constraints=ConstraintScore(passed=True),
        judgment=_semantic(rewrite_case.case_id, rewritten, JudgeVerdict.PASS),
    )
    live.append_case_attempt(_attempt(rewrite_case, result))

    candidates = build_audit_candidates(
        run_root=live.root,
        dataset_root=default_dataset_root(),
    )
    assert [candidate.case_id for candidate in candidates] == [rewrite_case.case_id]

    empty, _ = _store(tmp_path / "empty", Profile.PC_OPENAI_ACCEPTANCE)
    with pytest.raises(ValueError, match="no completed"):
        build_audit_candidates(run_root=empty.root, dataset_root=default_dataset_root())
