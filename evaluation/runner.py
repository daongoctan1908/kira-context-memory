"""Full-corpus benchmark planning and crash-safe sequential execution.

The runner owns dataset/profile policy and artifact durability.  Suite-specific runtime wiring is
injected through a small executor port so mock, company-PC and internal deployments cannot silently
substitute one another.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import Field

from evaluation.artifacts import (
    ArtifactRunIdentity,
    ArtifactStore,
    CaseAttemptArtifact,
    DiagnosticArtifact,
)
from evaluation.compiler import DatasetCompilation, compilation_json_bytes, compile_dataset
from evaluation.config import EvalConfig
from evaluation.dataset import DatasetManifest, load_manifest
from evaluation.models import (
    EvalCase,
    EvalModel,
    Identifier,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.scoring import output_sha256


class BenchmarkExecutionResult(EvalModel):
    """Small common result accepted from suite-specific evaluators."""

    case_id: Identifier
    outcome: Outcome
    reason_codes: tuple[Identifier, ...] = ()
    output: object | None = None


class BenchmarkCaseExecutor(Protocol):
    async def evaluate(self, case: EvalCase) -> EvalModel: ...


class BenchmarkRunPreparation(EvalModel):
    identity: ArtifactRunIdentity
    selected_cases: tuple[EvalCase, ...] = Field(min_length=1)


def validate_canonical_policy(manifest: DatasetManifest, profile: Profile) -> None:
    """Canonical dataset policy for any quality-profile evaluation consumer.

    MOCK bypasses dataset maturity; EXTERNAL_SYNTHETIC can never consume the
    canonical dataset; PC and INTERNAL_TEST both require a benchmark-ready dataset
    with frozen, materialized, reviewed bundles, and PC additionally requires the
    dataset-level external-provider approval. Evaluation-side consumers (including
    the D0-local runner) must reuse this function instead of restating policy."""
    if profile is Profile.EXTERNAL_SYNTHETIC:
        raise ValueError("external_synthetic cannot consume the canonical dataset")
    if profile is Profile.MOCK:
        return
    if manifest.status != "benchmark_ready":
        raise ValueError("quality profiles require a benchmark-ready dataset")
    if any(
        bundle.contract_status != "frozen"
        or bundle.materialization_status != "materialized"
        or bundle.review.status != "reviewed"
        for bundle in manifest.bundles
    ):
        raise ValueError("quality profiles require frozen, materialized and reviewed bundles")
    if (
        profile is Profile.PC_OPENAI_ACCEPTANCE
        and not manifest.data_policy.external_provider_allowed
    ):
        raise ValueError("canonical dataset is not approved for the PC external provider")


# Historical private name kept as an alias so existing callers are untouched.
_validate_canonical_policy = validate_canonical_policy


def prepare_benchmark_run(
    *,
    run_id: UUID,
    config: EvalConfig,
    provenance: RunProvenance,
    dataset_root: Path,
    seed: int,
    isolation_sha256: str | None = None,
) -> tuple[BenchmarkRunPreparation, DatasetCompilation]:
    """Validate policy and compile before any network or persistent runtime action."""

    manifest = load_manifest(dataset_root)
    _validate_canonical_policy(manifest, config.profile)
    compilation = compile_dataset(dataset_root, seed=seed)
    # The compiler deliberately shuffles the full corpus, but suite dependencies are not
    # interchangeable: formation must finish before formation-produced retrieval can be built,
    # and paired cross-session cases run last because they create their own durable jobs.  Keep
    # the compiler's deterministic order *within* each suite while enforcing that lifecycle.
    selected = tuple(
        case
        for suite in Suite
        if suite in config.suites
        for case in compilation.cases
        if case.suite is suite
    )
    if not selected:
        raise ValueError("selected suites contain no compiled cases")
    compilation_sha256 = sha256(compilation_json_bytes(compilation)).hexdigest()
    identity = ArtifactRunIdentity(
        run_id=run_id,
        profile=config.profile,
        variant=provenance.variant,
        provenance=provenance,
        dataset_id=compilation.dataset_id,
        dataset_version=compilation.dataset_version,
        dataset_sha256=compilation.dataset_sha256,
        compilation_sha256=compilation_sha256,
        config_sha256=config.fingerprint(),
        isolation_sha256=isolation_sha256,
        seed=seed,
        suites=config.suites,
        selected_case_ids=tuple(case.case_id for case in selected),
    )
    return BenchmarkRunPreparation(identity=identity, selected_cases=selected), compilation


def create_or_resume_store(
    root: Path,
    preparation: BenchmarkRunPreparation,
    *,
    now: datetime | None = None,
) -> ArtifactStore:
    if root.exists():
        return ArtifactStore.resume(root, expected_identity=preparation.identity)
    return ArtifactStore.create(
        root,
        identity=preparation.identity,
        created_at=now or datetime.now(UTC),
    )


async def execute_benchmark_cases(
    preparation: BenchmarkRunPreparation,
    store: ArtifactStore,
    executor: BenchmarkCaseExecutor,
) -> None:
    """Run unresolved cases sequentially; cancellation leaves the next case resumable."""

    if store.manifest.identity != preparation.identity:
        raise ValueError("artifact store belongs to another benchmark run")
    cases = {case.case_id: case for case in preparation.selected_cases}
    latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
    for case_id in store.resume_plan().run_case_ids:
        case = cases[case_id]
        previous = latest.get(case_id)
        if (
            case.eligibility.status == "blocked"
            and previous is not None
            and previous.outcome is Outcome.NOT_RUN
        ):
            continue
        attempt_number = store.next_attempt_number(case_id)
        try:
            result = await executor.evaluate(case)
            outcome = Outcome(result.outcome)
            reason_codes = tuple(getattr(result, "reason_codes", ()))
            output = (
                None
                if outcome in {Outcome.NOT_RUN, Outcome.DEPENDENCY_ERROR, Outcome.PROTOCOL_ERROR}
                else result.model_dump(mode="json", exclude_none=False)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = Outcome.PROTOCOL_ERROR
            reason_codes = ("benchmark_executor_unexpected_error",)
            output = None
        digest = output_sha256(output) if output is not None else None
        store.append_case_attempt(
            CaseAttemptArtifact(
                case_id=case.case_id,
                suite=case.suite,
                attempt=attempt_number,
                completed_at=datetime.now(UTC),
                outcome=outcome,
                output=output,
                output_sha256=digest,
                reason_codes=reason_codes,
            )
        )
        if outcome in {Outcome.DEPENDENCY_ERROR, Outcome.PROTOCOL_ERROR}:
            store.write_diagnostic(
                DiagnosticArtifact(
                    case_id=case.case_id,
                    attempt=attempt_number,
                    outcome=outcome,
                    reason_codes=reason_codes or ("benchmark_execution_failed",),
                )
            )


def benchmark_execution_complete(
    preparation: BenchmarkRunPreparation,
    store: ArtifactStore,
) -> bool:
    """Return true when each selected case has the expected execution-terminal artifact."""

    if store.manifest.identity != preparation.identity:
        raise ValueError("artifact store belongs to another benchmark run")
    latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
    quality_terminal = {
        Outcome.PASS,
        Outcome.FAIL,
        Outcome.REVIEW_REQUIRED,
        Outcome.INSUFFICIENT_EVIDENCE,
    }
    return all(
        (
            latest.get(case.case_id) is not None
            and (
                latest[case.case_id].outcome is Outcome.NOT_RUN
                if case.eligibility.status == "blocked"
                else latest[case.case_id].outcome in quality_terminal
            )
        )
        for case in preparation.selected_cases
    )


class MockBenchmarkExecutor:
    """Deterministic plumbing double; its outputs never claim semantic quality."""

    async def evaluate(self, case: EvalCase) -> BenchmarkExecutionResult:
        if case.eligibility.status == "blocked":
            return BenchmarkExecutionResult(
                case_id=case.case_id,
                outcome=Outcome.NOT_RUN,
                reason_codes=case.eligibility.blocked_reasons,
            )
        return BenchmarkExecutionResult(
            case_id=case.case_id,
            outcome=Outcome.REVIEW_REQUIRED,
            output={"simulated": True, "suite": case.suite.value},
        )
