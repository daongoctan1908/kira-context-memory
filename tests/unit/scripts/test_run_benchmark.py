"""Offline benchmark CLI commands and safe failure reporting."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from evaluation.artifacts import ArtifactRunIdentity, ArtifactStore, CaseAttemptArtifact
from evaluation.audit import AuditCandidate, AuditReconciliation
from evaluation.dataset import default_dataset_root
from evaluation.models import (
    BenchmarkVariant,
    GitSource,
    Outcome,
    PerformanceReviewVerdict,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.release_evidence import (
    CleanupEvidence,
    ExactImageSet,
    PerformanceEvidence,
    PerformanceSample,
    QualityMetric,
    QualityMetricName,
    RunQualityEvidence,
)
from evaluation.scoring import output_sha256
from evaluation.timing import TimingOutcome, TimingStage
from scripts.benchmark.run import (
    _load_application_settings,
    _require_native_run_contract,
    main,
)

_HASH = "a" * 64


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def _identity() -> ArtifactRunIdentity:
    source = GitSource(sha="1" * 40, dirty=False)
    provenance = RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": _HASH, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": "2.0.20+viettel.6",
        },
    )
    return ArtifactRunIdentity(
        run_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        profile=Profile.INTERNAL_TEST,
        variant=BenchmarkVariant.WORKING_TREE,
        provenance=provenance,
        dataset_id="kira-ltm-v1",
        dataset_version="1.0.0-test",
        dataset_sha256="c" * 64,
        compilation_sha256="d" * 64,
        config_sha256="e" * 64,
        seed=742,
        suites=(Suite.REWRITE,),
        selected_case_ids=("conv01:case-1", "conv01:case-2"),
    )


def _candidate(case_id: str, output_hash: str, verdict: str = "PASS") -> dict:
    return {
        "case_id": case_id,
        "bundle_id": "conv01",
        "suite": "rewrite",
        "variant": "working_tree",
        "output_sha256": output_hash,
        "judge_verdict": verdict,
        "judge_reason_code": "semantic_equivalent",
        "safety_passed": True,
    }


def test_validate_and_compile_canonical_dataset_offline(tmp_path: Path, capsys):
    validation = tmp_path / "validation.json"
    assert (
        main(
            [
                "validate",
                "--root",
                str(default_dataset_root()),
                "--output",
                str(validation),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is True
    assert json.loads(validation.read_text(encoding="utf-8")) == report

    compilation = tmp_path / "compilation.json"
    assert (
        main(
            [
                "compile",
                "--root",
                str(default_dataset_root()),
                "--seed",
                "742",
                "--output",
                str(compilation),
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    payload = json.loads(compilation.read_text(encoding="utf-8"))
    assert status["case_count"] == len(payload["cases"]) == 534
    assert payload["seed"] == 742
    assert payload["contract_id"] == "kira-week5-benchmark-v4"

    assert main(["compile", "--output", str(compilation)]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "offline_command_error"


def test_resume_cli_separates_pending_work_without_running_it(tmp_path: Path, capsys):
    root = tmp_path / "run"
    store = ArtifactStore.create(
        root,
        identity=_identity(),
        created_at=datetime(2026, 9, 18, tzinfo=UTC),
    )
    output = {"rewrite": "So sánh FTTH tại Hà Nội"}
    store.append_case_attempt(
        CaseAttemptArtifact(
            case_id="conv01:case-1",
            suite=Suite.REWRITE,
            attempt=1,
            completed_at=datetime(2026, 9, 18, tzinfo=UTC),
            outcome=Outcome.PASS,
            output=output,
            output_sha256=output_sha256(output),
        )
    )
    path = tmp_path / "resume.json"

    assert main(["resume", "--run-root", str(root), "--output", str(path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["ready_case_ids"] == ["conv01:case-1"]
    assert plan["run_case_ids"] == ["conv01:case-2"]
    assert json.loads(path.read_text(encoding="utf-8")) == plan


def test_audit_export_and_import_are_deterministic_and_hash_bound(tmp_path: Path, capsys):
    candidates_path = tmp_path / "candidates.jsonl"
    candidates = [
        _candidate("conv01:case-1", "1" * 64),
        _candidate("conv01:case-2", "2" * 64, "FAIL"),
    ]
    _write_jsonl(candidates_path, candidates)
    batch_path = tmp_path / "batch.json"

    assert (
        main(
            [
                "audit",
                "export",
                "--candidates",
                str(candidates_path),
                "--sample-rate",
                "1",
                "--output",
                str(batch_path),
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    assert status["audit_count"] == 2
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    assert len(batch["selections"]) == 2

    decisions_path = tmp_path / "decisions.jsonl"
    decisions = [
        {
            "case_id": selection["case_id"],
            "output_sha256": selection["output_sha256"],
            "reviewer": "mentor",
            "reviewed_at": "2026-09-18T09:00:00Z",
            "disposition": "verdict",
            "human_verdict": selection["judge_verdict"],
            "reason_code": "agrees_with_judge",
            "notes": "Reviewed against the frozen gold and synthetic output.",
        }
        for selection in batch["selections"]
    ]
    _write_jsonl(decisions_path, decisions)
    result_path = tmp_path / "reconciliation.json"

    assert (
        main(
            [
                "audit",
                "import",
                "--candidates",
                str(candidates_path),
                "--batch",
                str(batch_path),
                "--decisions",
                str(decisions_path),
                "--output",
                str(result_path),
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status["reconciled_count"] == 2
    assert result["pending_audit_ids"] == []


def test_audit_candidates_cli_writes_content_free_jsonl(tmp_path: Path, capsys, monkeypatch):
    candidate = AuditCandidate.model_validate(_candidate("conv01:case-1", "1" * 64))
    monkeypatch.setattr(
        "scripts.benchmark.run.build_audit_candidates",
        lambda **_kwargs: (candidate,),
    )
    output = tmp_path / "audit-candidates.jsonl"

    assert (
        main(
            [
                "audit",
                "candidates",
                "--run-root",
                str(tmp_path / "native-run"),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    status = json.loads(capsys.readouterr().out)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert status["candidate_count"] == 1
    assert payload["case_id"] == "conv01:case-1"
    assert "content" not in payload


def test_offline_cli_errors_are_sanitized(tmp_path: Path, capsys):
    bad = tmp_path / "candidates.jsonl"
    bad.write_text('{"secret":"sk-sensitive-provider-key"}', encoding="utf-8")

    assert (
        main(
            [
                "audit",
                "export",
                "--candidates",
                str(bad),
                "--output",
                str(tmp_path / "batch.json"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "sk-sensitive-provider-key" not in captured.out + captured.err
    assert json.loads(captured.out)["reason"] == "offline_command_error"


def test_run_cli_executes_and_resumes_the_mock_case_ledger(tmp_path: Path, capsys):
    provenance = tmp_path / "provenance.json"
    provenance.write_text(
        _identity().provenance.model_dump_json(exclude_computed_fields=True),
        encoding="utf-8",
    )
    root = tmp_path / "mock-run"
    arguments = [
        "run",
        "--profile",
        "mock",
        "--suite",
        "formation",
        "--suite",
        "retrieval",
        "--suite",
        "rewrite",
        "--suite",
        "cross_session",
        "--provenance-file",
        str(provenance),
        "--artifact-root",
        str(root),
    ]

    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    case_lines = (root / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    assert first["outcome"] == "PASS"
    assert first["quality_claim"] is False
    assert first["terminal_cases"] == len(case_lines) > 0

    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["run_id"] == first["run_id"]
    assert (root / "cases.jsonl").read_text(encoding="utf-8").splitlines() == case_lines


def test_native_run_requires_the_complete_persistent_suite_contract(tmp_path: Path, capsys):
    result = main(
        [
            "run",
            "--profile",
            "pc_openai_acceptance",
            "--suite",
            "rewrite",
            "--artifact-root",
            str(tmp_path / "pc-run"),
        ]
    )

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {"outcome": "NOT_RUN", "reason": "offline_command_error"}
    assert not (tmp_path / "pc-run").exists()

    with pytest.raises(ValueError, match="persistent"):
        _require_native_run_contract(
            Profile.PC_OPENAI_ACCEPTANCE,
            tuple(Suite),
            "write_free",
        )


def test_file_only_application_settings_ignore_ambient_values(tmp_path: Path, monkeypatch):
    env = tmp_path / "pc.env"
    env.write_text(
        "KIRA_BASE_URL=https://file-kira.invalid\n"
        "KIRA_USERNAME=file-user\n"
        "KIRA_BASIC_AUTH=file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KIRA_USERNAME", "ambient-user")

    settings = _load_application_settings(env, file_only=True)

    assert settings.kira_username == "file-user"
    assert settings.kira_basic_auth.get_secret_value() == "file-secret"


def _quality_evidence(variant: BenchmarkVariant, number: int, value: float) -> RunQualityEvidence:
    metrics = {name: QualityMetric(value=0.8, denominator=10) for name in QualityMetricName}
    metrics[QualityMetricName.FORMATION_F1] = QualityMetric(value=value, denominator=10)
    metrics[QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE] = QualityMetric(
        value=value, denominator=10
    )
    metrics[QualityMetricName.NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE] = QualityMetric(
        value=0.4, denominator=10
    )
    return RunQualityEvidence(
        run_id=UUID(int=number + (100 if variant is BenchmarkVariant.RELEASE_CANDIDATE else 0)),
        variant=variant,
        candidate_id="candidate-a" if variant is BenchmarkVariant.RELEASE_CANDIDATE else None,
        dataset_sha256="1" * 64,
        compilation_sha256="2" * 64,
        selected_case_ids_sha256="3" * 64,
        seed=number,
        runtime_sha=("4" if variant is BenchmarkVariant.HISTORICAL_CONTROL else "5") * 40,
        harness_sha="6" * 40,
        config_sha256=("7" if variant is BenchmarkVariant.HISTORICAL_CONTROL else "8") * 64,
        metrics=metrics,
        families=(),
        evidence_complete=True,
    )


def test_release_cli_builds_scorecard_confirmation_performance_and_promotion(
    tmp_path: Path, capsys, monkeypatch
):
    control_paths = []
    candidate_paths = []
    for index, candidate_value in enumerate((0.7, 0.5, 0.8), 1):
        control = _quality_evidence(BenchmarkVariant.HISTORICAL_CONTROL, index, 0.6)
        candidate = _quality_evidence(BenchmarkVariant.RELEASE_CANDIDATE, index, candidate_value)
        control_path = tmp_path / f"control-{index}.json"
        candidate_path = tmp_path / f"candidate-{index}.json"
        control_path.write_text(control.model_dump_json(), encoding="utf-8")
        candidate_path.write_text(candidate.model_dump_json(), encoding="utf-8")
        control_paths.append(control_path)
        candidate_paths.append(candidate_path)

    reconciliation = AuditReconciliation(
        candidate_set_sha256="a" * 64,
        cases=(),
        expansion=(),
        pending_audit_ids=(),
        insufficient_evidence_suites=(),
        dataset_revision_case_ids=(),
    )
    reconciliation_path = tmp_path / "reconciliation.json"
    reconciliation_path.write_text(reconciliation.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        "scripts.benchmark.run.build_run_quality_evidence",
        lambda **_kwargs: _quality_evidence(BenchmarkVariant.HISTORICAL_CONTROL, 1, 0.6),
    )
    scorecard_path = tmp_path / "scorecard.json"
    assert (
        main(
            [
                "release",
                "scorecard",
                "--run-root",
                str(tmp_path / "run"),
                "--audit-reconciliation",
                str(reconciliation_path),
                "--output",
                str(scorecard_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    confirmation_path = tmp_path / "confirmation.json"
    confirm_args = ["release", "confirm", "--component", "formation"]
    for path in control_paths:
        confirm_args.extend(("--control", str(path)))
    for path in candidate_paths:
        confirm_args.extend(("--candidate", str(path)))
    confirm_args.extend(("--output", str(confirmation_path)))
    assert main(confirm_args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "pass"

    samples = tuple(
        PerformanceSample(
            variant=variant,
            ordinal=ordinal,
            warmup=ordinal <= 5,
            duration_ms=float(ordinal),
            outcome=TimingOutcome.SUCCESS,
        )
        for variant in (
            BenchmarkVariant.HISTORICAL_CONTROL,
            BenchmarkVariant.RELEASE_CANDIDATE,
        )
        for ordinal in range(1, 36)
    )
    performance_evidence = PerformanceEvidence(
        stage=TimingStage.KIRA_COMPLETION,
        workload_sha256="b" * 64,
        environment_sha256="c" * 64,
        samples=samples,
    )
    evidence_path = tmp_path / "performance-evidence.json"
    evidence_path.write_text(performance_evidence.model_dump_json(), encoding="utf-8")
    performance_path = tmp_path / "performance.json"
    assert (
        main(
            [
                "release",
                "performance",
                "--confirmation",
                str(confirmation_path),
                "--evidence",
                str(evidence_path),
                "--verdict",
                PerformanceReviewVerdict.ACCEPTABLE,
                "--reviewer",
                "reviewer-1",
                "--rationale",
                "Bounded workload is acceptable.",
                "--output",
                str(performance_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    images = ExactImageSet(
        images={
            "control-runtime": f"registry/control-runtime@sha256:{'1' * 64}",
            "control-eval": f"registry/control-eval@sha256:{'2' * 64}",
            "candidate-runtime": f"registry/candidate-runtime@sha256:{'3' * 64}",
            "candidate-eval": f"registry/candidate-eval@sha256:{'4' * 64}",
        }
    )
    images_path = tmp_path / "images.json"
    images_path.write_text(images.model_dump_json(), encoding="utf-8")
    cleanup = CleanupEvidence(
        run_ids=tuple(UUID(int=index) for index in range(1, 7)), completed=True
    )
    cleanup_path = tmp_path / "cleanup.json"
    cleanup_path.write_text(cleanup.model_dump_json(), encoding="utf-8")
    promotion_path = tmp_path / "promotion.json"
    assert (
        main(
            [
                "release",
                "promote",
                "--confirmation",
                str(confirmation_path),
                "--performance",
                str(performance_path),
                "--images",
                str(images_path),
                "--cleanup",
                str(cleanup_path),
                "--decision",
                "promote_candidate",
                "--reviewer",
                "reviewer-1",
                "--rationale",
                "All release evidence passed.",
                "--output",
                str(promotion_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["outcome"] == "promote_candidate"
