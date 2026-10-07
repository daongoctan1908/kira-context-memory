"""Freeze sanitized company-PC dependency and KiRa preflight evidence."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from itertools import combinations
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
from evaluation.preflight import required_probes


class PcProviderIdentity(EvalModel):
    requested_model: str | None = None
    returned_model: str | None = None
    embedding_dimension: int | None = Field(default=None, ge=1)


class PcPreflightFreeze(EvalModel):
    schema_version: Literal[2] = 2
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    profile: Literal[Profile.PC_OPENAI_ACCEPTANCE] = Profile.PC_OPENAI_ACCEPTANCE
    official: Literal[False] = False
    ready: Literal[True] = True
    created_at: datetime
    dataset_id: str
    dataset_version: str
    dataset_sha256: Sha256
    config_sha256: Sha256
    selected_suites: tuple[Suite, ...] = Field(default=tuple(Suite), min_length=1)
    config_sha256_by_suites: dict[str, Sha256] = Field(default_factory=dict)
    provider_preflight_sha256: Sha256
    provider_run_id: UUID
    kira_response_sha256: Sha256 | None = None
    kira_identity_sha256: Sha256 | None = None
    kira_event_count: int | None = Field(default=None, ge=1)
    kira_text_bytes: int | None = Field(default=None, ge=1)
    kira_context_isolation: Literal["unique_username"] | None = "unique_username"
    providers: dict[Probe, PcProviderIdentity]
    provenance: RunProvenance

    @model_validator(mode="after")
    def suite_fingerprints_are_bound(self) -> "PcPreflightFreeze":
        _validate_suites(self.selected_suites)
        for key in self.config_sha256_by_suites:
            try:
                suites = tuple(Suite(value) for value in key.split(","))
            except ValueError as exc:
                raise ValueError("PC preflight has invalid suite config hash scope") from exc
            _validate_suites(suites)
            if not set(suites).issubset(self.selected_suites):
                raise ValueError("PC preflight suite config hash scope exceeds ready suites")
        selected_key = _suite_key(self.selected_suites)
        if self.config_sha256_by_suites and (
            self.config_sha256_by_suites.get(selected_key) != self.config_sha256
        ):
            raise ValueError("PC preflight selected-suite config hash is inconsistent")
        if Suite.CROSS_SESSION in self.selected_suites and not all(
            (
                self.kira_response_sha256,
                self.kira_identity_sha256,
                self.kira_event_count,
                self.kira_text_bytes,
                self.kira_context_isolation == "unique_username",
            )
        ):
            raise ValueError("PC preflight QA requires complete isolated real KiRa evidence")
        return self

    def config_sha256_for_suites(self, suites: tuple[Suite, ...]) -> str:
        """Project suite selection only; keep every other non-secret config setting bound."""
        _validate_suites(suites)
        if not set(suites).issubset(self.selected_suites):
            raise ValueError("PC preflight does not cover the selected suites")
        if not self.config_sha256_by_suites:
            return self.config_sha256
        expected = self.config_sha256_by_suites.get(_suite_key(suites))
        if expected is None:
            raise ValueError("PC preflight has no config hash for the selected suites")
        return expected


class PcPreflightRunSet(EvalModel):
    """Exact dependency evidence per variant; service endpoints are never normalized away."""

    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    profile: Literal[Profile.PC_OPENAI_ACCEPTANCE] = Profile.PC_OPENAI_ACCEPTANCE
    official: Literal[False] = False
    created_at: datetime
    dataset_sha256: Sha256
    variants: dict[Identifier, PcPreflightFreeze] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def exact_variants_are_bound(self) -> "PcPreflightRunSet":
        current_only = set(self.variants) == {"current"}
        if not current_only and (len(self.variants) < 2 or "control" not in self.variants):
            raise ValueError(
                "PC preflight requires current runtime or an explicit control comparison"
            )
        harnesses = {item.provenance.harness.sha for item in self.variants.values()}
        if len(harnesses) != 1:
            raise ValueError("PC preflight variants require the same exact harness")
        for variant_id, frozen in self.variants.items():
            if frozen.dataset_sha256 != self.dataset_sha256:
                raise ValueError("PC preflight variants are bound to different datasets")
            if current_only:
                if frozen.provenance.variant is not BenchmarkVariant.CURRENT_RUNTIME:
                    raise ValueError("current preflight requires current runtime provenance")
            elif variant_id == "control":
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


def _suite_key(suites: tuple[Suite, ...]) -> str:
    return ",".join(suite.value for suite in suites)


def _validate_suites(suites: tuple[Suite, ...]) -> None:
    if not suites or suites != tuple(suite for suite in Suite if suite in suites):
        raise ValueError("PC suite selection must be nonempty, unique and canonical")
    if Suite.RETRIEVAL in suites and Suite.FORMATION not in suites:
        raise ValueError("PC retrieval suite requires formation in the selected suites")


def _suite_config_fingerprints(config: EvalConfig) -> dict[str, str]:
    """Store hashes only, so suite reuse never persists KiRa usernames or credentials."""
    return {
        _suite_key(suites): config.model_copy(update={"suites": suites}).fingerprint()
        for count in range(1, len(config.suites) + 1)
        for suites in combinations(config.suites, count)
        if Suite.RETRIEVAL not in suites or Suite.FORMATION in suites
    }


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
    _validate_suites(sanitized_config.suites)
    if tuple(suite.suite for suite in report.suites) != sanitized_config.suites:
        raise ValueError("PC freeze suite readiness differs from its configuration")
    if any(
        suite.required_probes
        != required_probes(suite.suite, sanitized_config.formation_mode, report.profile)
        for suite in report.suites
    ):
        raise ValueError("PC freeze suite readiness has incorrect probe requirements")
    if any(suite.outcome is not Outcome.PASS for suite in report.suites):
        raise ValueError("PC provider preflight is not ready")
    required = {
        probe
        for suite in sanitized_config.suites
        for probe in required_probes(suite, sanitized_config.formation_mode, report.profile)
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
    kira = checks.get(Probe.KIRA_CHAT)
    if Suite.CROSS_SESSION in sanitized_config.suites and (
        kira is None
        or not kira.kira_response_sha256
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
        selected_suites=sanitized_config.suites,
        config_sha256_by_suites=_suite_config_fingerprints(sanitized_config),
        provider_preflight_sha256=_file_sha256(provider_preflight_path),
        provider_run_id=report.run_id,
        kira_response_sha256=kira.kira_response_sha256 if kira else None,
        kira_identity_sha256=kira.kira_identity_sha256 if kira else None,
        kira_event_count=kira.kira_event_count if kira else None,
        kira_text_bytes=kira.kira_text_bytes if kira else None,
        kira_context_isolation=kira.kira_context_isolation if kira else None,
        providers={
            probe: PcProviderIdentity(
                requested_model=checks[probe].requested_model,
                returned_model=checks[probe].returned_model,
                embedding_dimension=checks[probe].embedding_dimension,
            )
            for probe in provider_probes.intersection(required)
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
