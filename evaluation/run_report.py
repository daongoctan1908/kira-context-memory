"""Automatic descriptive benchmark report, without changing scoring or claiming promotion.

Quality uses latest case outputs. Operational measurements include all recorded attempts and each
shared source exactly once. PC semantic results are judge provisional, not human-audited release
evidence. Missing measurements and uncertain judgments are exposed rather than counted as zero.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from evaluation.artifacts import ArtifactRunManifest, ArtifactStore
from evaluation.compiler import compilation_json_bytes, compile_dataset
from evaluation.cross_session import CrossSessionCaseEvaluation
from evaluation.measurement import CaseMeasurements, ProviderCall, ProviderStage
from evaluation.models import EvalCase, EvalModel, Sha256, Suite
from evaluation.native_executor import NativeFormationCaseEvaluation, NativeRetrievalCaseEvaluation
from evaluation.release_evidence import (
    QualityMetric,
    QualityMetricName,
    _formation_metrics,
    _mean,
    is_acceptance_case,
)
from evaluation.rewrite import RewriteCaseEvaluation
from evaluation.scoring import JudgeVerdict
from evaluation.timing import TimingRecorder, TimingReport


class MemoryEffect(EvalModel):
    total_pairs: int
    resolved_pairs: int
    unresolved_pairs: int
    improved: int
    regressed: int
    both_pass: int
    both_fail: int
    net_uplift: float | None = None


class ProviderSummary(EvalModel):
    workload: Literal["shared_source", "formation", "retrieval", "rewrite", "cross_session"]
    stage: ProviderStage
    arm: Literal["no_ltm", "with_ltm"] | None = None
    basis: Literal["http_request", "sdk_invocation", "kira_chat"]
    observed_calls: int
    successful_calls: int
    error_calls: int
    calls_reporting_usage: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    observed_prompt_tokens: int
    observed_completion_tokens: int
    observed_total_tokens: int


class BenchmarkReport(EvalModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    runtime_sha: str
    harness_sha: str
    dataset_sha256: Sha256
    evaluation_scope: Literal["full_corpus", "selected_suites"]
    selected_suites: tuple[Suite, ...]
    selected_suite_coverage: Literal["full_selected_suites", "selected_case_subset"]
    quality_basis: Literal["latest_judge_outputs_unaudited"] = "latest_judge_outputs_unaudited"
    selected_cases: int
    attempted_cases: int
    total_attempts: int
    suite_outcomes: dict[Suite, dict[str, int]]
    metrics: dict[QualityMetricName, QualityMetric]
    diagnostic_metrics: dict[QualityMetricName, QualityMetric]
    memory_effect: MemoryEffect
    diagnostic_memory_effect: MemoryEffect
    safety_violation_codes: tuple[str, ...]
    source_bundles: int
    source_bundles_ready: int
    source_deliveries: int
    completed_source_deliveries: int
    missing_case_measurements: int
    missing_source_measurements: int
    provider_calls: tuple[ProviderSummary, ...]
    timing: dict[str, TimingReport] = Field(default_factory=dict)
    measurement_notes: tuple[str, ...] = (
        "Source deliveries are source events, not provider calls.",
        "SDK invocations do not reveal internal HTTP retries or batch splitting.",
        "Provider totals cover captured observations; absent stages and missing usage are N/A.",
        "Timings include recorded errors/timeouts and exclude unmeasured stages.",
        "Retrieval samples are scoped searches; KiRa timing excludes prior context/rewrite.",
        "Quality covers this run's selected cases; unrun component metrics are N/A.",
        "Quality is descriptive; technical acceptance and release approval are separate decisions.",
    )


def _verdict(judgment) -> bool | None:
    if judgment is None or judgment.verdict is JudgeVerdict.UNCERTAIN:
        return None
    return judgment.verdict is JudgeVerdict.PASS


def _quality(cases: tuple[EvalCase, ...], latest: dict) -> tuple[dict, MemoryEffect, set[str]]:
    formation = []
    samples = defaultdict(list)
    effects = Counter()
    safety: set[str] = set()
    for case in cases:
        if case.suite is Suite.CROSS_SESSION:
            effects["total"] += 1
        attempt = latest.get(case.case_id)
        if attempt is None or attempt.output is None:
            continue
        if case.suite is Suite.FORMATION:
            formation.append(NativeFormationCaseEvaluation.model_validate(attempt.output))
        elif case.suite is Suite.RETRIEVAL:
            result = NativeRetrievalCaseEvaluation.model_validate(attempt.output)
            if result.formation_produced is not None:
                safety.update(result.formation_produced.safety_violation_codes)
                score = result.formation_produced.score
                if score is not None:
                    if score.recall_at_3 is not None:
                        samples[QualityMetricName.RETRIEVAL_RECALL_AT_3].append(score.recall_at_3)
                    if score.reciprocal_rank is not None:
                        samples[QualityMetricName.RETRIEVAL_MRR_AT_10].append(score.reciprocal_rank)
        elif case.suite is Suite.REWRITE:
            result = RewriteCaseEvaluation.model_validate(attempt.output)
            if result.constraints is not None:
                samples[QualityMetricName.REWRITE_CONSTRAINT_PASS_RATE].append(
                    float(result.constraints.passed)
                )
            verdict = _verdict(result.judgment)
            if verdict is not None:
                samples[QualityMetricName.REWRITE_SEMANTIC_PASS_RATE].append(float(verdict))
        else:
            result = CrossSessionCaseEvaluation.model_validate(attempt.output)
            no_ltm, with_ltm = result.no_ltm, result.with_ltm
            if no_ltm is None or with_ltm is None:
                continue
            for arm, semantic_metric, task_metric in (
                (
                    no_ltm,
                    QualityMetricName.NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE,
                    QualityMetricName.NO_LTM_TASK_SUCCESS_RATE,
                ),
                (
                    with_ltm,
                    QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE,
                    QualityMetricName.FINAL_QA_TASK_SUCCESS_RATE,
                ),
            ):
                safety.update(arm.safety_violation_codes)
                semantic = _verdict(arm.final_judgment)
                if semantic is not None:
                    samples[semantic_metric].append(float(semantic))
                if arm.task_success is not None:
                    samples[task_metric].append(float(arm.task_success.passed))
            before, after = _verdict(no_ltm.final_judgment), _verdict(with_ltm.final_judgment)
            if before is not None and after is not None:
                effects["resolved"] += 1
                effects[(before, after)] += 1
    metrics = {name: _mean(samples[name]) for name in QualityMetricName}
    metrics.update(_formation_metrics(formation))
    return (
        metrics,
        MemoryEffect(
            total_pairs=effects["total"],
            resolved_pairs=effects["resolved"],
            unresolved_pairs=effects["total"] - effects["resolved"],
            improved=effects[(False, True)],
            regressed=effects[(True, False)],
            both_pass=effects[(True, True)],
            both_fail=effects[(False, False)],
            net_uplift=(effects[(False, True)] - effects[(True, False)]) / effects["resolved"]
            if effects["resolved"]
            else None,
        ),
        safety,
    )


def _operations(
    measurements: dict[str, list[CaseMeasurements]],
) -> tuple[tuple[ProviderSummary, ...], dict[str, TimingReport]]:
    groups: dict[tuple, list[ProviderCall]] = defaultdict(list)
    timing: dict[str, TimingRecorder] = {}
    for workload, records in measurements.items():
        for measurement in records:
            for call in measurement.provider_calls:
                groups[(workload, call.stage, call.arm, call.basis)].append(call)
            for arm, report in measurement.timing.items():
                recorder = timing.setdefault(f"{workload}:{arm}", TimingRecorder())
                for attempt in report.attempts:
                    recorder.record(**attempt.model_dump(exclude={"attempt"}))
    providers = []
    for (workload, stage, arm, basis), calls in sorted(
        groups.items(), key=lambda row: tuple(str(value) for value in row[0])
    ):
        totals = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            observed = [
                getattr(call.usage, field)
                for call in calls
                if call.usage is not None and getattr(call.usage, field) is not None
            ]
            totals[field] = sum(observed) if len(observed) == len(calls) else None
            totals[f"observed_{field}"] = sum(observed)
        providers.append(
            ProviderSummary(
                workload=workload,
                stage=stage,
                arm=arm,
                basis=basis,
                observed_calls=len(calls),
                successful_calls=sum(call.outcome == "success" for call in calls),
                error_calls=sum(call.outcome == "error" for call in calls),
                calls_reporting_usage=sum(call.usage is not None for call in calls),
                **totals,
            )
        )
    return tuple(providers), {arm: recorder.report() for arm, recorder in timing.items()}


def build_benchmark_report(*, run_root: Path, dataset_root: Path) -> BenchmarkReport:
    manifest = ArtifactRunManifest.model_validate_json(
        (run_root / "manifest.json").read_text(encoding="utf-8")
    )
    identity = manifest.identity
    store = ArtifactStore.resume(run_root, expected_identity=identity)
    compilation = compile_dataset(dataset_root, seed=identity.seed)
    if compilation.dataset_sha256 != identity.dataset_sha256:
        raise ValueError("report dataset differs from the benchmark run")
    if sha256(compilation_json_bytes(compilation)).hexdigest() != identity.compilation_sha256:
        raise ValueError("report compilation differs from the benchmark run")
    by_id = {case.case_id: case for case in compilation.cases}
    selected = tuple(by_id[case_id] for case_id in identity.selected_case_ids)
    selected_suites = tuple(suite for suite in Suite if suite in identity.suites)
    selected_ids = set(identity.selected_case_ids)
    selected_suite_ids = {
        case.case_id for case in compilation.cases if case.suite in selected_suites
    }
    if not selected_ids.issubset(selected_suite_ids):
        raise ValueError("report selected cases fall outside the selected suites")
    latest = {attempt.case_id: attempt for attempt in store.latest_attempts}
    quality, effect, safety = _quality(
        tuple(case for case in selected if is_acceptance_case(case)), latest
    )
    diagnostic_quality, diagnostic_effect, diagnostic_safety = _quality(
        tuple(
            case
            for case in selected
            if case.eligibility.status == "eligible" and not is_acceptance_case(case)
        ),
        latest,
    )
    measurements: dict[str, list[CaseMeasurements]] = defaultdict(list)
    for attempt in store.attempts:
        if attempt.measurement is not None:
            measurements[attempt.suite.value].append(attempt.measurement)
    bundle_ids = sorted(
        {
            case.case_id.split(":")[0]
            for case in selected
            if case.suite is Suite.CROSS_SESSION and case.eligibility.status == "eligible"
        }
    )
    sources = []
    for bundle_id in bundle_ids:
        try:
            sources.append(store.load_bundle_source(bundle_id))
        except FileNotFoundError:
            pass
    measurements["shared_source"].extend(
        source.measurement for source in sources if source.measurement is not None
    )
    providers, timing = _operations(measurements)
    return BenchmarkReport(
        run_id=identity.run_id,
        runtime_sha=identity.provenance.runtime.sha,
        harness_sha=identity.provenance.harness.sha,
        dataset_sha256=identity.dataset_sha256,
        evaluation_scope=(
            "full_corpus"
            if selected_ids == set(by_id) and selected_suites == tuple(Suite)
            else "selected_suites"
        ),
        selected_suites=selected_suites,
        selected_suite_coverage=(
            "full_selected_suites" if selected_ids == selected_suite_ids else "selected_case_subset"
        ),
        selected_cases=len(selected),
        attempted_cases=len(latest),
        total_attempts=len(store.attempts),
        suite_outcomes={
            suite: dict(
                Counter(
                    latest[case.case_id].outcome.value if case.case_id in latest else "missing"
                    for case in selected
                    if case.suite is suite
                )
            )
            for suite in identity.suites
        },
        metrics=quality,
        diagnostic_metrics=diagnostic_quality,
        memory_effect=effect,
        diagnostic_memory_effect=diagnostic_effect,
        safety_violation_codes=tuple(sorted(safety | diagnostic_safety)),
        source_bundles=len(bundle_ids),
        source_bundles_ready=sum(source.ready for source in sources),
        source_deliveries=sum(len(source.events) for source in sources),
        completed_source_deliveries=sum(
            event.completed for source in sources for event in source.events
        ),
        missing_case_measurements=sum(attempt.measurement is None for attempt in store.attempts),
        missing_source_measurements=len(bundle_ids)
        - sum(source.measurement is not None for source in sources),
        provider_calls=providers,
        timing=timing,
    )
