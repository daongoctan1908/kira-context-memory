"""Offline handoff acceptance stays deterministic and credential-free."""

import json
from pathlib import Path

import pytest

from scripts.week5_mock_acceptance import run_mock_acceptance

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_mock_acceptance_builds_complete_offline_bundle(tmp_path: Path):
    output = tmp_path / "mock-acceptance"

    result = await run_mock_acceptance(output)

    assert result.case_count > 0
    assert set(result.suite_outcomes) == {
        "formation",
        "retrieval",
        "rewrite",
        "cross_session",
    }
    assert set(result.suite_outcomes.values()) == {"PASS"}
    assert {path.name for path in output.iterdir()} == {
        "dataset-validation.json",
        "compiled-cases.json",
        "mock-preflight.json",
        "mock-acceptance.json",
    }
    summary = json.loads((output / "mock-acceptance.json").read_text(encoding="utf-8"))
    assert summary["contract_id"] == "kira-week5-benchmark-v4"
    assert summary["network_required"] is False
    assert summary["quality_claim"] is False
    assert summary["materialization_checkpoint"] == ("artifacts/week5/kira-materialization.json")

    serialized = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    for forbidden in ("api_key", "password", "Authorization", "Bearer ", "sk-"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_mock_acceptance_refuses_to_overwrite_existing_evidence(tmp_path: Path):
    output = tmp_path / "mock-acceptance"
    await run_mock_acceptance(output)

    with pytest.raises(FileExistsError):
        await run_mock_acceptance(output)


def test_eval_runtime_is_locked_and_does_not_install_or_download_at_startup():
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.eval").read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM python:3.11-slim-bookworm AS runtime", maxsplit=1)[1]

    assert "uv sync --frozen --no-dev --extra evaluation --no-editable" in dockerfile
    assert "ARG SOURCE_REVISION" in runtime
    assert 'io.kira.benchmark.role="eval-controller"' in runtime
    assert "scripts/review_dataset.py" in dockerfile
    for forbidden in ("pip install", "uv sync", "curl ", "wget ", "model download"):
        assert forbidden not in runtime


def test_internal_compose_uses_only_prebuilt_images_and_fixed_handoff_mounts():
    compose = (REPOSITORY_ROOT / "compose.week5.benchmark.yaml").read_text(encoding="utf-8")

    assert "build:" not in compose
    assert compose.count("pull_policy: never") == 11
    assert "WEEK5_MATERIALIZATION_ROOT" in compose
    assert ":/materialization" in compose
    assert "WEEK5_DATASET_ROOT" in compose
    assert ":/app/dataset/kira_ltm_v1" in compose
    assert 'OTEL_ENABLED: "false"' in compose


def test_handoff_script_pins_control_and_verifies_offline_bundle():
    script = (REPOSITORY_ROOT / "scripts/week5_offline_handoff.ps1").read_text(encoding="utf-8-sig")

    assert "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00" in script
    assert '"--pull=false"' in script
    assert '@("save", "--output", $archive)' in script
    assert '@("load", "--input", $archive)' in script
    assert '"--network", "none"' in script
    assert "Get-FileHash" in script
    assert "org.opencontainers.image.revision" in script


def test_internal_env_template_contains_placeholders_not_populated_credentials():
    template = (REPOSITORY_ROOT / "evaluation/week5.internal.env.example").read_text(
        encoding="utf-8"
    )

    assert "replace_me" in template
    assert "WEEK5_CONTROL_IMAGE=" in template
    assert "WEEK5_CANDIDATE_IMAGE=" in template
    assert "WEEK5_EVAL_IMAGE=" in template
    assert "OPENAI_API_KEY=" not in template
    assert "sk-" not in template
