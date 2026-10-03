"""Company-PC preflight freeze binds sanitized provider and real-KiRa evidence."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from anyio import Path as AsyncPath
from pydantic import AnyHttpUrl, SecretStr

from evaluation.config import EvalConfig, ProviderConfig
from evaluation.dataset import default_dataset_root
from evaluation.mock import mock_database, mock_response
from evaluation.models import (
    HISTORICAL_CONTROL_SHA,
    BenchmarkVariant,
    CandidateChangeScope,
    CandidateDeclaration,
    GitSource,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.pc_preflight import freeze_pc_preflight, freeze_pc_preflight_run_set
from evaluation.preflight import run_preflight
from scripts.benchmark.freeze_pc_preflight import main as freeze_main


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
        kira_base_url="https://kira.test",
        kira_username="approved-benchmark-base",
        kira_domain="VBI",
        kira_basic_auth=SecretStr("synthetic-kira-credential"),
        kira_context_isolation="unique_username",
    )


def _real_kira_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/authenticate":
        return httpx.Response(200, json={"errorCode": "00", "content": "synthetic-runtime-token"})
    if request.url.path == "/api/v1/chat":
        payload = {
            "data": {"response": [{"type": "text", "content": {"text": "raw KiRa response"}}]}
        }
        return httpx.Response(200, content=f"data: {json.dumps(payload)}\n\n")
    return mock_response(request)


async def _write_provider_report(
    path: Path,
    *,
    judge_configured: bool = True,
    config: EvalConfig | None = None,
    provenance: RunProvenance | None = None,
) -> None:
    config = config or _config()
    if not judge_configured:
        config = config.model_copy(update={"judge": ProviderConfig()})
    report = await run_preflight(
        config,
        transport=httpx.MockTransport(_real_kira_response),
        database_probe=mock_database,
        provenance=provenance or _provenance(),
    )
    await AsyncPath(path).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


async def test_freeze_binds_complete_sanitized_pc_preflight(tmp_path: Path):
    root = _approved_dataset(tmp_path)
    provider_path = tmp_path / "provider-preflight.json"
    await _write_provider_report(provider_path)

    frozen = freeze_pc_preflight(
        provider_preflight_path=provider_path,
        dataset_root=root,
        now=datetime(2026, 9, 18, tzinfo=UTC),
    )

    serialized = frozen.model_dump_json()
    assert frozen.ready and not frozen.official
    assert frozen.schema_version == 2
    assert frozen.kira_event_count == 1
    assert frozen.kira_text_bytes == len(b"raw KiRa response")
    assert frozen.providers
    assert frozen.providers[next(iter(frozen.providers))].requested_model == "explicit-model"
    assert "raw KiRa response" not in serialized
    assert "synthetic-secret-token" not in serialized
    assert "synthetic-kira-credential" not in serialized
    assert "synthetic-runtime-token" not in serialized
    assert "approved-benchmark-base" not in serialized
    assert "password" not in serialized


async def test_freeze_rejects_missing_judge(tmp_path: Path):
    root = _approved_dataset(tmp_path)
    provider_path = tmp_path / "provider-preflight.json"
    await _write_provider_report(provider_path, judge_configured=False)

    with pytest.raises(ValueError):
        freeze_pc_preflight(
            provider_preflight_path=provider_path,
            dataset_root=root,
        )


@pytest.mark.parametrize("mutation", ["missing", "simulated", "zero_text", "disabled", "config"])
async def test_freeze_rejects_incomplete_or_unbound_current_kira_evidence(tmp_path, mutation):
    root = _approved_dataset(tmp_path)
    provider_path = tmp_path / "provider-preflight.json"
    await _write_provider_report(provider_path)
    document = json.loads(provider_path.read_text(encoding="utf-8"))
    check = next(item for item in document["checks"] if item["probe"] == "kira_real_chat")
    if mutation == "missing":
        document["checks"].remove(check)
    if mutation == "simulated":
        check["simulated"] = True
    if mutation == "zero_text":
        check["kira_text_bytes"] = None
    if mutation == "disabled":
        check["kira_context_isolation"] = None
    if mutation == "config":
        document["config_sha256"] = "a" * 64
    provider_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        freeze_pc_preflight(provider_preflight_path=provider_path, dataset_root=root)


def _variant_provenance(variant_id):
    control = variant_id == "control"
    return _provenance().model_copy(
        update={
            "variant": BenchmarkVariant.HISTORICAL_CONTROL
            if control
            else BenchmarkVariant.RELEASE_CANDIDATE,
            "runtime": GitSource(sha=HISTORICAL_CONTROL_SHA if control else "2" * 40, dirty=False),
            "package_versions": {
                "kira-context-memory": "0.4.1",
                "viettel-mem0": "2.0.20+viettel.3" if control else "2.0.20+viettel.6",
            },
            "candidate": None
            if control
            else CandidateDeclaration(
                candidate_id=variant_id,
                change_scopes=(CandidateChangeScope.RUNTIME_CODE,),
                summary="Exact candidate preflight.",
            ),
        }
    )


async def test_run_set_keeps_real_control_and_candidate_target_fingerprints(tmp_path):
    root = _approved_dataset(tmp_path)
    paths = {}
    configs = {}
    for variant_id, target in (("control", "control"), ("candidate-a", "candidate")):
        configs[variant_id] = _config().model_copy(
            update={
                "gateway_url": AnyHttpUrl(f"http://{target}-gateway:8000"),
                "worker_url": AnyHttpUrl(f"http://{target}-worker:8001"),
            }
        )
        paths[variant_id] = tmp_path / f"{variant_id}.json"
        await _write_provider_report(
            paths[variant_id],
            config=configs[variant_id],
            provenance=_variant_provenance(variant_id),
        )
    frozen = freeze_pc_preflight_run_set(provider_preflight_paths=paths, dataset_root=root)
    assert set(frozen.variants) == {"control", "candidate-a"}
    assert frozen.variants["control"].config_sha256 != frozen.variants["candidate-a"].config_sha256
    assert all(
        frozen.variants[name].config_sha256 == config.fingerprint()
        for name, config in configs.items()
    )
    serialized = frozen.model_dump_json(exclude_computed_fields=True)
    assert "synthetic-runtime-token" not in serialized and "raw KiRa response" not in serialized
    output = tmp_path / "run-set-freeze.json"
    assert (
        freeze_main(
            [
                "--provider-preflight",
                f"control={paths['control']}",
                "--provider-preflight",
                f"candidate-a={paths['candidate-a']}",
                "--dataset-root",
                str(root),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert set(json.loads(output.read_text(encoding="utf-8"))["variants"]) == set(paths)


async def test_run_set_rejects_missing_control_and_mislabeled_candidate(tmp_path):
    root = _approved_dataset(tmp_path)
    paths = {}
    for name in ("control", "candidate-a"):
        paths[name] = tmp_path / f"{name}.json"
        await _write_provider_report(paths[name], provenance=_variant_provenance(name))
    with pytest.raises(ValueError):
        freeze_pc_preflight_run_set(
            provider_preflight_paths={"candidate-a": paths["candidate-a"]},
            dataset_root=root,
        )
    with pytest.raises(ValueError, match="declared identity"):
        freeze_pc_preflight_run_set(
            provider_preflight_paths={
                "control": paths["control"],
                "candidate-b": paths["candidate-a"],
            },
            dataset_root=root,
        )
