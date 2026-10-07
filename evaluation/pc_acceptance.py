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
from evaluation.compiler import compile_dataset, cross_session_source_sha256
from evaluation.isolation import allocate_bundle_resources, kira_benchmark_username
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    BenchmarkVariant,
    CrossSessionInput,
    EvalCase,
    EvalModel,
    GitSha,
    Identifier,
    Outcome,
    Profile,
    Sha256,
    Suite,
)
from evaluation.pc_preflight import PcPreflightRunSet

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
    config_sha256: Sha256
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
    schema_version: Literal[2] = 2
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    profile: Literal[Profile.PC_OPENAI_ACCEPTANCE] = Profile.PC_OPENAI_ACCEPTANCE
    official: Literal[False] = False
    quality_decision: Literal["diagnostic_only_no_promotion"] = "diagnostic_only_no_promotion"
    created_at: datetime
    dataset_sha256: Sha256
    image_manifest_sha256: Sha256
    pc_preflight_sha256: Sha256
    selected_suites: tuple[Suite, ...] = Field(default=tuple(Suite), min_length=1)
    evaluation_scope: Literal["full_corpus", "selected_suites"] = "full_corpus"
    variants: tuple[PcVariantAcceptance, ...] = Field(min_length=1, max_length=3)
    technical_passed: bool

    @model_validator(mode="after")
    def overall_state_is_consistent(self) -> "PcAcceptanceManifest":
        if self.selected_suites != tuple(suite for suite in Suite if suite in self.selected_suites):
            raise ValueError("PC acceptance suites must be unique and canonical")
        if Suite.RETRIEVAL in self.selected_suites and Suite.FORMATION not in self.selected_suites:
            raise ValueError("PC acceptance retrieval suite requires formation")
        expected_scope = (
            "full_corpus" if self.selected_suites == tuple(Suite) else "selected_suites"
        )
        if self.evaluation_scope != expected_scope:
            raise ValueError("PC acceptance scope differs from its selected suites")
        if self.technical_passed != all(item.technical_passed for item in self.variants):
            raise ValueError("PC acceptance verdict is inconsistent")
        ids = [item.variant_id for item in self.variants]
        if ids == ["current"]:
            if self.variants[0].benchmark_variant is not BenchmarkVariant.CURRENT_RUNTIME:
                raise ValueError("current acceptance requires current runtime provenance")
        elif len(ids) < 2 or ids[0] != "control" or len(ids) != len(set(ids)):
            raise ValueError(
                "PC acceptance requires current runtime or an explicit control comparison"
            )
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


def _validate_shared_sources(store: ArtifactStore, cases: Mapping[str, EvalCase]) -> set[str]:
    """A terminal QA must be bound to the entire independently formed, run-owned source."""

    source_cases: dict[str, list[EvalCase]] = {}
    for case_id in store.manifest.identity.selected_case_ids:
        case = cases[case_id]
        if case.suite is Suite.CROSS_SESSION:
            source_cases.setdefault(case_id.split(":", 1)[0], []).append(case)
    if not source_cases:
        return set()
    incomplete: set[str] = set()
    plan = store.load_isolation_plan()
    ledger = store.load_isolation_ledger()
    sources = {
        resource.bundle_id: resource
        for resource in ledger.resources
        if resource.resource_role == "bundle_source"
    }
    qa_resources = {
        (resource.case_id, resource.attempt): resource
        for resource in ledger.resources
        if resource.resource_role == "bundle_qa"
    }
    latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
    for bundle_id, bundle_cases in source_cases.items():
        try:
            corpus = store.load_bundle_source(bundle_id)
        except FileNotFoundError:
            incomplete.update(case.case_id for case in bundle_cases)
            continue
        inputs = bundle_cases[0].inputs
        assert isinstance(inputs, CrossSessionInput)
        expected_hash = cross_session_source_sha256(inputs)
        if any(cross_session_source_sha256(case.inputs) != expected_hash for case in bundle_cases):
            raise ValueError("QA bundle does not have one immutable source trajectory")
        if (
            corpus.source_sha256 != expected_hash
            or corpus.logical_user_id != inputs.user_id
            or corpus.expected_source_events != len(inputs.session_a_messages) // 2
        ):
            raise ValueError("bundle source does not match its complete compiled trajectory")
        source = sources.get(bundle_id)
        expected = allocate_bundle_resources(plan, bundle_id=bundle_id)
        if source is None or (
            source.user_id != expected.user_id
            or source.session_id != expected.session_id
            or corpus.persisted_user_id != source.user_id
            or corpus.source_session_id != source.session_id
            or (
                corpus.ready
                and corpus.source_conversation_id is not None
                and corpus.source_conversation_id != source.conversation_id
            )
            or (corpus.ready and corpus.events and source.event_id != corpus.events[-1].event_id)
        ):
            raise ValueError("bundle source is not bound to its exact resource owner")
        memory_ids = {memory_id for event in corpus.events for memory_id in event.memory_ids}
        if memory_ids != set(source.memory_ids):
            raise ValueError("bundle memory evidence differs from its durable ownership ledger")
        if not corpus.ready or corpus.failed:
            incomplete.update(case.case_id for case in bundle_cases)
        for case in bundle_cases:
            attempt = latest.get(case.case_id)
            if attempt is None or attempt.outcome not in _QUALITY_TERMINAL:
                continue
            qa = qa_resources.get((case.case_id, attempt.attempt))
            if qa is None or qa.bundle_id != bundle_id or qa.user_id != source.user_id:
                raise ValueError("QA context does not belong to its source bundle user")
            for arm in ("no_ltm", "with_ltm"):
                expected_username = kira_benchmark_username(
                    plan, case_id=case.case_id, attempt=attempt.attempt, arm=arm
                )
                payload = attempt.output.get(arm) if isinstance(attempt.output, Mapping) else None
                if not isinstance(payload, Mapping) or payload.get(
                    "kira_context_identity_sha256"
                ) != (sha256(expected_username.encode()).hexdigest()):
                    raise ValueError(
                        "QA KiRa context differs from its isolated arm/attempt identity"
                    )
    return incomplete


def build_pc_acceptance(
    *,
    run_roots: Mapping[str, Path],
    dataset_root: Path,
    image_manifest_path: Path,
    pc_preflight_path: Path,
    now: datetime | None = None,
) -> PcAcceptanceManifest:
    """Evaluate technical gates without comparing or promoting quality metrics."""

    current_only = set(run_roots) == {"current"}
    if not current_only and (not 2 <= len(run_roots) <= 3 or "control" not in run_roots):
        raise ValueError("PC acceptance requires current runtime or an explicit control comparison")
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

    preflight = PcPreflightRunSet.model_validate_json(pc_preflight_path.read_text(encoding="utf-8"))
    if preflight.dataset_sha256 != compilation.dataset_sha256:
        raise ValueError("PC preflight is bound to another dataset or is not ready")
    image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
    if image_manifest.get("contract_id") != BENCHMARK_CONTRACT_ID:
        raise ValueError("image manifest uses another benchmark contract")
    declared = {
        item.get("variant_id"): item
        for item in image_manifest.get("variants", [])
        if isinstance(item, dict)
    }
    expected_variant_ids = set(run_roots)
    if set(declared) != expected_variant_ids:
        raise ValueError("run variants do not match the exact image manifest")
    if set(preflight.variants) != expected_variant_ids:
        raise ValueError("run variants do not match the exact PC preflight run set")

    variant_results: list[PcVariantAcceptance] = []
    common_case_ids: tuple[str, ...] | None = None
    common_suites: tuple[Suite, ...] | None = None
    ordered_ids = (
        ("current",)
        if current_only
        else ("control", *sorted(key for key in run_roots if key != "control"))
    )
    for variant_id in ordered_ids:
        store = _load_store(run_roots[variant_id])
        identity = store.manifest.identity
        variant_preflight = preflight.variants[variant_id]
        if identity.profile is not Profile.PC_OPENAI_ACCEPTANCE or store.manifest.official:
            raise ValueError("run is not non-official PC acceptance evidence")
        if identity.dataset_sha256 != compilation.dataset_sha256:
            raise ValueError("run dataset hash differs from PC freeze")
        if identity.config_sha256 != variant_preflight.config_sha256_for_suites(identity.suites):
            raise ValueError("run config hash differs from PC freeze")
        if common_suites is None:
            common_suites = identity.suites
        elif identity.suites != common_suites:
            raise ValueError("PC variants must run the same selected suites")
        if common_case_ids is None:
            common_case_ids = identity.selected_case_ids
        elif identity.selected_case_ids != common_case_ids:
            raise ValueError("PC variants must run the same ordered corpus")
        selected_ids = {case.case_id for case in compilation.cases if case.suite in identity.suites}
        if set(identity.selected_case_ids) != selected_ids:
            raise ValueError(
                "PC variant did not select the complete compiled corpus for its suites"
            )
        eligible_ids = set().union(*(eligible_by_suite[suite] for suite in identity.suites))
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
        if identity.provenance.harness.sha != variant_preflight.provenance.harness.sha:
            raise ValueError("run harness SHA differs from current PC preflight")
        if identity.provenance != variant_preflight.provenance:
            raise ValueError("run provenance differs from its exact variant preflight")
        if identity.provenance.prompt_sha256 != metadata.get("prompt_sha256"):
            raise ValueError("run prompt hashes differ from exact image manifest")
        if identity.provenance.package_versions != metadata.get("package_versions"):
            raise ValueError("run package versions differ from exact image manifest")
        expected_kind = (
            BenchmarkVariant.CURRENT_RUNTIME
            if current_only
            else BenchmarkVariant.HISTORICAL_CONTROL
            if variant_id == "control"
            else BenchmarkVariant.RELEASE_CANDIDATE
        )
        if identity.variant is not expected_kind:
            raise ValueError("run variant kind differs from image declaration")

        latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
        incomplete_source_cases = _validate_shared_sources(store, cases)
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
            if (
                attempt is None
                or attempt.outcome not in _QUALITY_TERMINAL
                or case_id in incomplete_source_cases
            ):
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
                config_sha256=identity.config_sha256,
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

    assert common_suites is not None
    return PcAcceptanceManifest(
        created_at=now or datetime.now(UTC),
        dataset_sha256=compilation.dataset_sha256,
        image_manifest_sha256=_file_sha256(image_manifest_path),
        pc_preflight_sha256=_file_sha256(pc_preflight_path),
        selected_suites=common_suites,
        evaluation_scope="full_corpus" if common_suites == tuple(Suite) else "selected_suites",
        variants=tuple(variant_results),
        technical_passed=all(item.technical_passed for item in variant_results),
    )
