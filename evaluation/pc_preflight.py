"""Freeze sanitized company-PC dependency and KiRa preflight evidence."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from evaluation.compiler import compile_dataset
from evaluation.config import EvalConfig
from evaluation.dataset import load_manifest
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    BenchmarkVariant,
    EvalModel,
    Identifier,
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
    schema_version: Literal[2] = 2
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
    provider_run_id: UUID
    kira_response_sha256: Sha256
    kira_identity_sha256: Sha256
    kira_event_count: int = Field(ge=1)
    kira_text_bytes: int = Field(ge=1)
    kira_context_isolation: Literal["unique_username"] = "unique_username"
    providers: dict[Probe, PcProviderIdentity]
    provenance: RunProvenance


class PcPreflightRunSet(EvalModel):
    """Exact dependency evidence per variant; service endpoints are never normalized away."""

    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    profile: Literal[Profile.PC_OPENAI_ACCEPTANCE] = Profile.PC_OPENAI_ACCEPTANCE
    official: Literal[False] = False
    created_at: datetime
    dataset_sha256: Sha256
    variants: dict[Identifier, PcPreflightFreeze] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def exact_variants_are_bound(self) -> "PcPreflightRunSet":
        if "control" not in self.variants:
            raise ValueError("PC preflight run set requires control and candidates")
        harnesses = {item.provenance.harness.sha for item in self.variants.values()}
        if len(harnesses) != 1:
            raise ValueError("PC preflight variants require the same exact harness")
        for variant_id, frozen in self.variants.items():
            if frozen.dataset_sha256 != self.dataset_sha256:
                raise ValueError("PC preflight variants are bound to different datasets")
            if variant_id == "control":
                if frozen.provenance.variant is not BenchmarkVariant.HISTORICAL_CONTROL:
                    raise ValueError("control preflight requires historical control provenance")
            elif (
                frozen.provenance.variant is not BenchmarkVariant.RELEASE_CANDIDATE
                or frozen.provenance.candidate is None
                or frozen.provenance.candidate.candidate_id != variant_id
            ):
                raise ValueError("candidate preflight differs from its declared identity")
        return self


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def freeze_pc_preflight(
    *,
    provider_preflight_path: Path,
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
    sanitized_config = EvalConfig.model_validate(report.configuration)
    if sanitized_config.fingerprint() != report.config_sha256:
        raise ValueError("PC preflight configuration fingerprint is inconsistent")
    if (
        sanitized_config.profile is not Profile.PC_OPENAI_ACCEPTANCE
        or sanitized_config.kira_mock_url is not None
    ):
        raise ValueError("PC preflight must use the declared real KiRa configuration")
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
        Probe.KIRA_CHAT,
    }
    checks = {check.probe: check for check in report.checks}
    if len(checks) != len(report.checks):
        raise ValueError("PC preflight contains duplicate probe evidence")
    if required.difference(checks) or any(
        checks[probe].outcome is not Outcome.PASS
        or not checks[probe].attempted
        or checks[probe].simulated
        for probe in required
    ):
        raise ValueError("PC provider preflight is missing a required successful probe")
    if report.provenance.runtime.dirty or report.provenance.harness.dirty:
        raise ValueError("PC preflight provenance must be clean")

    manifest = load_manifest(dataset_root)
    if manifest.status != "benchmark_ready" or not manifest.data_policy.external_provider_allowed:
        raise ValueError("PC freeze requires the reviewed external-approved dataset")
    compilation = compile_dataset(dataset_root, seed=742)
    kira = checks[Probe.KIRA_CHAT]
    if (
        not kira.kira_response_sha256
        or not kira.kira_identity_sha256
        or not kira.kira_event_count
        or not kira.kira_text_bytes
        or kira.kira_context_isolation != "unique_username"
        or report.configuration.get("kira_context_isolation") != "unique_username"
        or not report.configuration.get("kira_base_url")
    ):
        raise ValueError(
            "PC freeze requires current-run isolated real KiRa authentication and chat"
        )

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
        provider_run_id=report.run_id,
        kira_response_sha256=kira.kira_response_sha256,
        kira_identity_sha256=kira.kira_identity_sha256,
        kira_event_count=kira.kira_event_count,
        kira_text_bytes=kira.kira_text_bytes,
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


def freeze_pc_preflight_run_set(
    *,
    provider_preflight_paths: Mapping[str, Path],
    dataset_root: Path,
    now: datetime | None = None,
) -> PcPreflightRunSet:
    created_at = now or datetime.now(UTC)
    variants = {
        variant_id: freeze_pc_preflight(
            provider_preflight_path=path,
            dataset_root=dataset_root,
            now=created_at,
        )
        for variant_id, path in provider_preflight_paths.items()
    }
    if not variants:
        raise ValueError("PC preflight run set is empty")
    return PcPreflightRunSet(
        created_at=created_at,
        dataset_sha256=next(iter(variants.values())).dataset_sha256,
        variants=variants,
    )
