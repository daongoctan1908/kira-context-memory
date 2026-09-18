"""Freeze sanitized company-PC dependency and KiRa preflight evidence."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from evaluation.compiler import compile_dataset
from evaluation.dataset import load_manifest
from evaluation.materialization import (
    load_materialization_checkpoint,
)
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    EvalModel,
    Outcome,
    PreflightReport,
    Probe,
    Profile,
    RunProvenance,
    Sha256,
    Suite,
)


class PcProviderIdentity(EvalModel):
    requested_model: str | None = None
    returned_model: str | None = None
    embedding_dimension: int | None = Field(default=None, ge=1)


class PcPreflightFreeze(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    profile: Literal[Profile.PC_OPENAI_ACCEPTANCE] = Profile.PC_OPENAI_ACCEPTANCE
    official: Literal[False] = False
    ready: Literal[True] = True
    created_at: datetime
    dataset_id: str
    dataset_version: str
    dataset_sha256: Sha256
    config_sha256: Sha256
    provider_preflight_sha256: Sha256
    materialization_checkpoint_sha256: Sha256
    kira_completed_tasks: int = Field(ge=1)
    providers: dict[Probe, PcProviderIdentity]
    provenance: RunProvenance


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def freeze_pc_preflight(
    *,
    provider_preflight_path: Path,
    materialization_checkpoint_path: Path,
    dataset_root: Path,
    now: datetime | None = None,
) -> PcPreflightFreeze:
    """Fail closed unless both sanitized provider and real-KiRa evidence are complete."""

    report_payload = json.loads(provider_preflight_path.read_text(encoding="utf-8"))
    # ``RunProvenance.attribution_scope`` is a derived display field in emitted artifacts, not an
    # input field. Drop it before strict validation instead of relaxing the core model.
    if isinstance(report_payload, dict) and isinstance(report_payload.get("provenance"), dict):
        report_payload["provenance"].pop("attribution_scope", None)
    report = PreflightReport.model_validate(report_payload)
    if report.profile is not Profile.PC_OPENAI_ACCEPTANCE or report.simulated:
        raise ValueError("PC freeze requires a non-simulated PC acceptance preflight")
    if {suite.suite for suite in report.suites} != set(Suite):
        raise ValueError("PC freeze requires all benchmark suites")
    if any(suite.outcome is not Outcome.PASS for suite in report.suites):
        raise ValueError("PC provider preflight is not ready")
    required = {
        Probe.EXTRACTION_JSON,
        Probe.REWRITE_CHAT,
        Probe.JUDGE_SEMANTIC,
        Probe.EMBEDDING_BATCH,
        Probe.PGVECTOR,
        Probe.MEMORY_SCHEMA,
        Probe.CONVERSATION_DB,
        Probe.GATEWAY,
        Probe.WORKER,
    }
    checks = {check.probe: check for check in report.checks}
    if required.difference(checks) or any(
        checks[probe].outcome is not Outcome.PASS or not checks[probe].attempted
        for probe in required
    ):
        raise ValueError("PC provider preflight is missing a required successful probe")
    if report.provenance.runtime.dirty or report.provenance.harness.dirty:
        raise ValueError("PC preflight provenance must be clean")

    manifest = load_manifest(dataset_root)
    if manifest.status != "benchmark_ready" or not manifest.data_policy.external_provider_allowed:
        raise ValueError("PC freeze requires the reviewed external-approved dataset")
    compilation = compile_dataset(dataset_root, seed=742)
    checkpoint = load_materialization_checkpoint(materialization_checkpoint_path)
    if checkpoint.total_count != 80 or checkpoint.completed_count != checkpoint.total_count:
        raise ValueError("PC freeze requires the complete real KiRa materialization checkpoint")

    provider_probes = {
        Probe.EXTRACTION_JSON,
        Probe.REWRITE_CHAT,
        Probe.JUDGE_SEMANTIC,
        Probe.EMBEDDING_BATCH,
    }
    return PcPreflightFreeze(
        created_at=now or datetime.now(UTC),
        dataset_id=compilation.dataset_id,
        dataset_version=compilation.dataset_version,
        dataset_sha256=compilation.dataset_sha256,
        config_sha256=report.config_sha256,
        provider_preflight_sha256=_file_sha256(provider_preflight_path),
        materialization_checkpoint_sha256=_file_sha256(materialization_checkpoint_path),
        kira_completed_tasks=checkpoint.completed_count,
        providers={
            probe: PcProviderIdentity(
                requested_model=checks[probe].requested_model,
                returned_model=checks[probe].returned_model,
                embedding_dimension=checks[probe].embedding_dimension,
            )
            for probe in provider_probes
        },
        provenance=report.provenance,
    )
