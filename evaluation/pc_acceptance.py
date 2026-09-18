"""Technical company-PC acceptance gate; quality metrics remain diagnostic only."""

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from evaluation.artifacts import ArtifactRunManifest, ArtifactStore
from evaluation.compiler import compile_dataset
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    BenchmarkVariant,
    EvalModel,
    GitSha,
    Identifier,
    Outcome,
    Profile,
    Sha256,
    Suite,
)
from evaluation.pc_preflight import PcPreflightFreeze

_QUALITY_TERMINAL = {
    Outcome.PASS,
    Outcome.FAIL,
    Outcome.REVIEW_REQUIRED,
    Outcome.INSUFFICIENT_EVIDENCE,
}


class PcVariantAcceptance(EvalModel):
    variant_id: Identifier
    benchmark_variant: BenchmarkVariant
    runtime_revision: GitSha
    run_id: str
    eligible_cases: int = Field(ge=1)
    completed_eligible_cases: int = Field(ge=0)
    outcomes: dict[Outcome, int]
    unresolved_case_ids: tuple[Identifier, ...] = ()
    safety_violation_codes: tuple[Identifier, ...] = ()
    technical_passed: bool

    @model_validator(mode="after")
    def technical_state_is_consistent(self) -> "PcVariantAcceptance":
        expected = (
            self.completed_eligible_cases == self.eligible_cases
            and not self.unresolved_case_ids
            and not self.safety_violation_codes
        )
        if self.technical_passed != expected:
            raise ValueError("variant technical verdict is inconsistent")
        return self


class PcAcceptanceManifest(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    profile: Literal[Profile.PC_OPENAI_ACCEPTANCE] = Profile.PC_OPENAI_ACCEPTANCE
    official: Literal[False] = False
    quality_decision: Literal["diagnostic_only_no_promotion"] = "diagnostic_only_no_promotion"
    created_at: datetime
    dataset_sha256: Sha256
    config_sha256: Sha256
    image_manifest_sha256: Sha256
    pc_preflight_sha256: Sha256
    variants: tuple[PcVariantAcceptance, ...] = Field(min_length=2, max_length=3)
    technical_passed: bool

    @model_validator(mode="after")
    def overall_state_is_consistent(self) -> "PcAcceptanceManifest":
        if self.technical_passed != all(item.technical_passed for item in self.variants):
            raise ValueError("PC acceptance verdict is inconsistent")
        ids = [item.variant_id for item in self.variants]
        if ids[0] != "control" or len(ids) != len(set(ids)):
            raise ValueError("PC acceptance requires one control followed by unique candidates")
        return self


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _safety_codes(value: object) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                key == "safety_violation_codes"
                and isinstance(child, Sequence)
                and not isinstance(child, (str, bytes))
            ):
                found.extend(code for code in child if isinstance(code, str))
            else:
                found.extend(_safety_codes(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found.extend(_safety_codes(child))
    return tuple(dict.fromkeys(found))


def _load_store(root: Path) -> ArtifactStore:
    manifest = ArtifactRunManifest.model_validate_json(
        (root / "manifest.json").read_text(encoding="utf-8")
    )
    return ArtifactStore.resume(root, expected_identity=manifest.identity)


def build_pc_acceptance(
    *,
    run_roots: Mapping[str, Path],
    dataset_root: Path,
    image_manifest_path: Path,
    pc_preflight_path: Path,
    now: datetime | None = None,
) -> PcAcceptanceManifest:
    """Evaluate technical gates without comparing or promoting quality metrics."""

    if not 2 <= len(run_roots) <= 3 or "control" not in run_roots:
        raise ValueError("PC acceptance requires control and one or two candidates")
    compilation = compile_dataset(dataset_root, seed=742)
    cases = {case.case_id: case for case in compilation.cases}
    eligible_by_suite = {
        suite: {
            case.case_id
            for case in compilation.cases
            if case.suite is suite and case.eligibility.status == "eligible"
        }
        for suite in Suite
    }
    eligible_ids = set().union(*eligible_by_suite.values())

    preflight = PcPreflightFreeze.model_validate_json(pc_preflight_path.read_text(encoding="utf-8"))
    if preflight.dataset_sha256 != compilation.dataset_sha256 or not preflight.ready:
        raise ValueError("PC preflight is bound to another dataset or is not ready")
    image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
    if image_manifest.get("contract_id") != BENCHMARK_CONTRACT_ID:
        raise ValueError("image manifest uses another benchmark contract")
    declared = {
        item.get("variant_id"): item
        for item in image_manifest.get("variants", [])
        if isinstance(item, dict)
    }
    expected_variant_ids = {"control", *(key for key in run_roots if key != "control")}
    if set(declared) != expected_variant_ids:
        raise ValueError("run variants do not match the exact image manifest")

    variant_results: list[PcVariantAcceptance] = []
    common_case_ids: tuple[str, ...] | None = None
    ordered_ids = ("control", *sorted(key for key in run_roots if key != "control"))
    for variant_id in ordered_ids:
        store = _load_store(run_roots[variant_id])
        identity = store.manifest.identity
        if identity.profile is not Profile.PC_OPENAI_ACCEPTANCE or store.manifest.official:
            raise ValueError("run is not non-official PC acceptance evidence")
        if identity.dataset_sha256 != compilation.dataset_sha256:
            raise ValueError("run dataset hash differs from PC freeze")
        if identity.config_sha256 != preflight.config_sha256:
            raise ValueError("run config hash differs from PC freeze")
        if set(identity.suites) != set(Suite):
            raise ValueError("PC acceptance run must include all four suites")
        if common_case_ids is None:
            common_case_ids = identity.selected_case_ids
        elif identity.selected_case_ids != common_case_ids:
            raise ValueError("PC variants must run the same ordered corpus")
        if set(identity.selected_case_ids) != set(cases):
            raise ValueError("PC variant did not select the complete compiled corpus")
        expected_runtime = declared[variant_id].get("runtime_revision")
        metadata = declared[variant_id].get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("image manifest is missing eval-image metadata")
        if (
            metadata.get("dataset_id") != compilation.dataset_id
            or metadata.get("dataset_version") != compilation.dataset_version
            or metadata.get("dataset_sha256") != compilation.dataset_sha256
        ):
            raise ValueError("eval image is bound to another dataset")
        if identity.provenance.runtime.sha != expected_runtime:
            raise ValueError("run runtime SHA differs from exact image manifest")
        if identity.provenance.harness.sha != metadata.get("harness_revision"):
            raise ValueError("run harness SHA differs from exact image manifest")
        if identity.provenance.prompt_sha256 != metadata.get("prompt_sha256"):
            raise ValueError("run prompt hashes differ from exact image manifest")
        if identity.provenance.package_versions != metadata.get("package_versions"):
            raise ValueError("run package versions differ from exact image manifest")
        expected_kind = (
            BenchmarkVariant.HISTORICAL_CONTROL
            if variant_id == "control"
            else BenchmarkVariant.RELEASE_CANDIDATE
        )
        if identity.variant is not expected_kind:
            raise ValueError("run variant kind differs from image declaration")

        latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
        unresolved: list[str] = []
        safety: list[str] = []
        completed = 0
        outcomes: Counter[Outcome] = Counter()
        for case_id in identity.selected_case_ids:
            attempt = latest.get(case_id)
            if attempt is not None:
                outcomes[attempt.outcome] += 1
            if case_id not in eligible_ids:
                if attempt is None or attempt.outcome is not Outcome.NOT_RUN:
                    unresolved.append(case_id)
                continue
            if attempt is None or attempt.outcome not in _QUALITY_TERMINAL:
                unresolved.append(case_id)
                continue
            completed += 1
            safety.extend(_safety_codes(attempt.output))
        unique_safety = tuple(dict.fromkeys(safety))
        variant_results.append(
            PcVariantAcceptance(
                variant_id=variant_id,
                benchmark_variant=identity.variant,
                runtime_revision=identity.provenance.runtime.sha,
                run_id=str(identity.run_id),
                eligible_cases=len(eligible_ids),
                completed_eligible_cases=completed,
                outcomes=dict(outcomes),
                unresolved_case_ids=tuple(unresolved),
                safety_violation_codes=unique_safety,
                technical_passed=completed == len(eligible_ids)
                and not unresolved
                and not unique_safety,
            )
        )

    return PcAcceptanceManifest(
        created_at=now or datetime.now(UTC),
        dataset_sha256=compilation.dataset_sha256,
        config_sha256=preflight.config_sha256,
        image_manifest_sha256=_file_sha256(image_manifest_path),
        pc_preflight_sha256=_file_sha256(pc_preflight_path),
        variants=tuple(variant_results),
        technical_passed=all(item.technical_passed for item in variant_results),
    )
