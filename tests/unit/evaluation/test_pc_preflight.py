"""Company-PC preflight freeze binds sanitized provider and real-KiRa evidence."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from anyio import Path as AsyncPath
from pydantic import SecretStr

from evaluation.config import EvalConfig, ProviderConfig
from evaluation.dataset import default_dataset_root
from evaluation.materialization import (
    build_materialization_checkpoint,
    completed_task,
    replace_checkpoint_task,
    write_materialization_checkpoint,
)
from evaluation.mock import mock_database, mock_response
from evaluation.models import BenchmarkVariant, GitSource, Profile, RunProvenance, Suite
from evaluation.pc_preflight import freeze_pc_preflight
from evaluation.preflight import run_preflight


def _approved_dataset(tmp_path: Path) -> Path:
    root = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), root)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["status"] = "benchmark_ready"
    manifest["data_policy"]["external_provider_allowed"] = True
    for bundle in manifest["bundles"]:
        bundle["contract_status"] = "frozen"
        bundle["materialization_status"] = "materialized"
        bundle["review"] = {"status": "reviewed", "reviewer": "mentor", "revision": "gold-v1"}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return root


def _provenance() -> RunProvenance:
    source = GitSource(sha="1" * 40, dirty=False)
    return RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": "2.0.20+viettel.6",
        },
    )


def _config() -> EvalConfig:
    provider = ProviderConfig(
        base_url="https://provider.test/v1",
        model="explicit-model",
        api_key=SecretStr("synthetic-secret-token"),
        auth_required=True,
    )
    return EvalConfig(
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        suites=tuple(Suite),
        formation_mode="persistent",
        extraction=provider,
        rewrite=provider,
        embedding=provider,
        judge=provider,
        embedding_dimensions=3,
        database_url=SecretStr("postgresql://user:password@db/eval"),
        memory_database_url=SecretStr("postgresql://user:password@db/eval"),
        gateway_url="https://gateway.test",
        worker_url="https://worker.test",
    )


def _complete_checkpoint(path: Path) -> None:
    current = build_materialization_checkpoint(default_dataset_root())
    for index, task in enumerate(current.tasks):
        current = replace_checkpoint_task(
            current,
            index,
            completed_task(task, f"raw KiRa response {index}"),
        )
    write_materialization_checkpoint(path, current)


async def _write_provider_report(path: Path, *, judge_configured: bool = True) -> None:
    config = _config()
    if not judge_configured:
        config = config.model_copy(update={"judge": ProviderConfig()})
    report = await run_preflight(
        config,
        transport=httpx.MockTransport(mock_response),
        database_probe=mock_database,
        provenance=_provenance(),
    )
    await AsyncPath(path).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


async def test_freeze_binds_complete_sanitized_pc_preflight(tmp_path: Path):
    root = _approved_dataset(tmp_path)
    provider_path = tmp_path / "provider-preflight.json"
    checkpoint_path = tmp_path / "kira-materialization.json"
    await _write_provider_report(provider_path)
    _complete_checkpoint(checkpoint_path)

    frozen = freeze_pc_preflight(
        provider_preflight_path=provider_path,
        materialization_checkpoint_path=checkpoint_path,
        dataset_root=root,
        now=datetime(2026, 9, 18, tzinfo=UTC),
    )

    serialized = frozen.model_dump_json()
    assert frozen.ready and not frozen.official
    assert frozen.kira_completed_tasks == 80
    assert frozen.providers
    assert frozen.providers[next(iter(frozen.providers))].requested_model == "explicit-model"
    assert "raw KiRa response" not in serialized
    assert "synthetic-secret-token" not in serialized
    assert "password" not in serialized


async def test_freeze_rejects_missing_judge_or_incomplete_kira(tmp_path: Path):
    root = _approved_dataset(tmp_path)
    provider_path = tmp_path / "provider-preflight.json"
    checkpoint_path = tmp_path / "kira-materialization.json"
    await _write_provider_report(provider_path, judge_configured=False)
    write_materialization_checkpoint(
        checkpoint_path,
        build_materialization_checkpoint(default_dataset_root()),
    )

    with pytest.raises(ValueError):
        freeze_pc_preflight(
            provider_preflight_path=provider_path,
            materialization_checkpoint_path=checkpoint_path,
            dataset_root=root,
        )
