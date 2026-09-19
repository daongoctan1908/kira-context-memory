"""Offline handoff acceptance stays deterministic and credential-free."""

import json
from pathlib import Path

import pytest

from scripts.benchmark_image_metadata import image_metadata
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

    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "ARG SOURCE_REVISION" in runtime
    assert "ARG RUNTIME_REVISION" in runtime
    assert "ARG HARNESS_REVISION" in runtime
    assert "ARG BENCHMARK_ROLE=eval-controller" in runtime
    assert 'io.kira.benchmark.runtime-revision="${RUNTIME_REVISION}"' in runtime
    assert 'io.kira.benchmark.harness-revision="${HARNESS_REVISION}"' in runtime
    assert 'io.kira.benchmark.role="${BENCHMARK_ROLE}"' in runtime
    assert 'test -n "${RUNTIME_REVISION}"' in runtime
    assert 'test -n "${HARNESS_REVISION}"' in runtime
    assert "COPY --from=variant_source app ./app" in dockerfile
    assert "COPY --from=variant_source packages/viettel-mem0/mem0" in dockerfile
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
    assert "[ValidateCount(1, 2)]" in script
    assert "schema_version = 2" in script
    assert 'Get-ImageRecord "control-eval"' in script
    assert '"$($candidate.variant_id)-eval"' in script
    assert "runtime_revision = $ExpectedRuntimeRevision" in script
    assert "Where-Object { $_.variant_id -eq $VariantId }" in script
    assert '"--build-context", "variant_source=$controlPath"' in script
    assert "Get-EvalMetadata" in script
    assert "foreach ($variant in $manifest.variants)" in script
    assert '"-m", "scripts.freeze_handoff"' in script
    assert "Assert-ManifestImagesUnchanged" in script
    assert "Assert-BundleFileHashes" in script
    assert "PcAcceptancePath" in script
    assert "dataset_manifest_sha256" in script
    assert "bundle-manifest.json" in script
    assert "K8S-RUNBOOK.md" in script
    assert "CandidateChangeScope" in script
    assert 'provenance_file = "provenance/control.json"' in script
    assert 'variant = "historical_control"' in script
    assert 'variant = "release_candidate"' in script
    assert "Export checkout differs from the exact accepted harness revision" in script
    assert "registry-manifest.json" in script
    assert "immutable_reference = $digests[0]" in script
    assert "source_image_manifest_sha256 = Get-Sha256 $ManifestPath" in script
    assert "Registry manifest already exists" in script


def test_image_metadata_binds_dataset_prompts_packages_and_exact_revisions(monkeypatch):
    monkeypatch.setenv("BENCHMARK_RUNTIME_REVISION", "1" * 40)
    monkeypatch.setenv("BENCHMARK_HARNESS_REVISION", "2" * 40)

    metadata = image_metadata(require_frozen=False)

    assert metadata["contract_id"] == "kira-week5-benchmark-v4"
    assert metadata["runtime_revision"] == "1" * 40
    assert metadata["harness_revision"] == "2" * 40
    assert metadata["dataset_id"] == "kira_ltm_v1"
    assert len(metadata["dataset_sha256"]) == 64
    assert set(metadata["prompt_sha256"]) == {"memory_extraction", "rewrite_system"}
    assert set(metadata["package_versions"]) == {
        "kira-context-memory",
        "viettel-mem0",
    }


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


def test_pc_env_template_requires_explicit_provider_and_kira_configuration():
    template = (REPOSITORY_ROOT / "evaluation/week5.pc.env.example").read_text(encoding="utf-8")

    for name in (
        "WEEK5_EXTRACTION_BASE_URL",
        "WEEK5_REWRITE_BASE_URL",
        "WEEK5_EMBEDDING_BASE_URL",
        "WEEK5_JUDGE_BASE_URL",
        "KIRA_BASE_URL",
        "KIRA_BASIC_AUTH",
    ):
        assert f"{name}=" in template
    assert "WEEK5_OPENAI_BASE_URL=" not in template
    assert "OPENAI_API_KEY=" not in template
    assert "sk-" not in template
