"""Offline benchmark CLI commands and safe failure reporting."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from evaluation.artifacts import ArtifactRunIdentity, ArtifactStore, CaseAttemptArtifact
from evaluation.dataset import default_dataset_root
from evaluation.models import (
    BenchmarkVariant,
    GitSource,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.scoring import output_sha256
from scripts.run_week5_benchmark import main

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
            "viettel-mem0": "2.0.20+viettel.4",
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
    assert result["pending_audit_case_ids"] == []


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


def test_run_cli_refuses_to_fake_a_live_native_executor(tmp_path: Path, capsys):
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
    assert output == {
        "outcome": "NOT_RUN",
        "profile": "pc_openai_acceptance",
        "reason": "native_benchmark_executor_not_configured",
    }
    assert not (tmp_path / "pc-run").exists()
