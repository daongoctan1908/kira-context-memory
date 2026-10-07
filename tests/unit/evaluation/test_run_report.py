"""Automatic reports consume measured evidence without converting uncertainty into quality."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from evaluation.artifacts import (
    BundleSourceArtifact,
    BundleSourceEventArtifact,
    CaseAttemptArtifact,
)
from evaluation.config import load_config
from evaluation.cross_session import (
    CrossSessionArmEvaluation,
    CrossSessionCaseEvaluation,
    CrossSessionCondition,
)
from evaluation.dataset import default_dataset_root
from evaluation.measurement import (
    MeasurementRecorder,
    ProviderStage,
    measurement_arm,
    record_provider_call,
)
from evaluation.models import BenchmarkVariant, GitSource, Outcome, Profile, RunProvenance, Suite
from evaluation.release_evidence import QualityMetric, QualityMetricName, is_acceptance_case
from evaluation.run_report import _operations, build_benchmark_report
from evaluation.runner import BenchmarkRunPreparation, create_or_resume_store, prepare_benchmark_run
from evaluation.scoring import (
    ConstraintScore,
    JudgeProvenance,
    JudgeVerdict,
    SemanticJudgment,
    TaskSuccessScore,
    output_sha256,
)


def _result(case_id, before, after, *, score_tasks=False):
    arms = []
    for condition, verdict in (
        (CrossSessionCondition.NO_LTM, before),
        (CrossSessionCondition.WITH_LTM, after),
    ):
        arms.append(
            CrossSessionArmEvaluation(
                case_id=case_id,
                condition=condition,
                outcome=Outcome.REVIEW_REQUIRED,
                rewritten_query="query",
                final_answer="answer",
                rewrite_constraints=ConstraintScore(passed=True),
                final_judgment=SemanticJudgment(
                    case_id=case_id,
                    suite=Suite.CROSS_SESSION,
                    output_sha256=output_sha256("answer"),
                    verdict=verdict,
                    reason_code="test",
                    rationale="synthetic judgment",
                    judge=JudgeProvenance(
                        provider="test",
                        model="test",
                        prompt_sha256="a" * 64,
                        response_schema_sha256="b" * 64,
                    ),
                ),
                task_success=(
                    TaskSuccessScore(
                        passed=verdict is JudgeVerdict.PASS,
                        action_matches=verdict is JudgeVerdict.PASS,
                    )
                    if score_tasks
                    else None
                ),
            )
        )
    return CrossSessionCaseEvaluation(
        case_id=case_id, outcome=Outcome.REVIEW_REQUIRED, no_ltm=arms[0], with_ltm=arms[1]
    )


def _prepare_run(suites=(Suite.CROSS_SESSION,)):
    config = load_config(profile=Profile.MOCK, suites=suites)
    source = GitSource(sha="1" * 40, dirty=False)
    provenance = RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={"kira-context-memory": "0.4.1", "viettel-mem0": "2.0.20+viettel.7"},
    )
    return prepare_benchmark_run(
        run_id=uuid4(),
        config=config,
        provenance=provenance,
        dataset_root=default_dataset_root(),
        seed=742,
    )


def test_automatic_report_uses_latest_quality_all_attempt_calls_and_source_once(tmp_path):
    preparation, _ = _prepare_run()
    selected = tuple(
        case
        for case in preparation.selected_cases
        if case.case_id.startswith("conv01:") and is_acceptance_case(case)
    )[:4]
    preparation = BenchmarkRunPreparation(
        identity=preparation.identity.model_copy(
            update={"selected_case_ids": tuple(case.case_id for case in selected)}
        ),
        selected_cases=selected,
    )
    store = create_or_resume_store(tmp_path / "run", preparation)
    failed = MeasurementRecorder()
    with failed.bind():
        record_provider_call(ProviderStage.JUDGE, basis="http_request", outcome="error")
    store.append_case_attempt(
        CaseAttemptArtifact(
            case_id=selected[0].case_id,
            suite=Suite.CROSS_SESSION,
            attempt=1,
            completed_at=datetime.now(UTC),
            outcome=Outcome.DEPENDENCY_ERROR,
            measurement=failed.snapshot(),
        )
    )
    for case, verdicts in zip(
        selected,
        (
            (JudgeVerdict.FAIL, JudgeVerdict.PASS),
            (JudgeVerdict.PASS, JudgeVerdict.FAIL),
            (JudgeVerdict.PASS, JudgeVerdict.PASS),
            (JudgeVerdict.UNCERTAIN, JudgeVerdict.PASS),
        ),
        strict=True,
    ):
        measured = MeasurementRecorder()
        with measured.bind():
            for arm in ("no_ltm", "with_ltm"):
                with measurement_arm(arm):
                    record_provider_call(
                        ProviderStage.JUDGE,
                        basis="http_request",
                        outcome="success",
                        usage={"input": 10, "output": 2, "total": 12},
                    )
        output = _result(case.case_id, *verdicts).model_dump(mode="json")
        store.append_case_attempt(
            CaseAttemptArtifact(
                case_id=case.case_id,
                suite=Suite.CROSS_SESSION,
                attempt=store.next_attempt_number(case.case_id),
                completed_at=datetime.now(UTC),
                outcome=Outcome.REVIEW_REQUIRED,
                output=output,
                output_sha256=output_sha256(output),
                measurement=measured.snapshot(),
            )
        )
    source_measure = MeasurementRecorder()
    with source_measure.bind():
        record_provider_call(
            ProviderStage.EXTRACTION, basis="sdk_invocation", outcome="success", usage={"total": 25}
        )
    conversation_id = uuid4()
    store.write_bundle_source(
        BundleSourceArtifact(
            identity=preparation.identity,
            bundle_id="conv01",
            logical_user_id=selected[0].inputs.user_id,
            source_sha256="c" * 64,
            persisted_user_id="source-user",
            source_session_id="source-conversation",
            source_conversation_id=conversation_id,
            expected_source_events=1,
            events=(
                BundleSourceEventArtifact(
                    event_id=uuid4(),
                    user_id="source-user",
                    session_id="source-conversation",
                    conversation_id=conversation_id,
                    turn_id="source-turn",
                    boundary_message_id=2,
                    completed=True,
                ),
            ),
            ready=True,
            measurement=source_measure.snapshot(),
        )
    )
    report = build_benchmark_report(run_root=store.root, dataset_root=default_dataset_root())
    assert report.evaluation_scope == "selected_suites"
    assert report.selected_suites == (Suite.CROSS_SESSION,)
    assert report.selected_suite_coverage == "selected_case_subset"
    assert report.selected_cases == report.attempted_cases == 4
    assert report.total_attempts == 5
    assert report.memory_effect.model_dump() == dict(
        total_pairs=4,
        resolved_pairs=3,
        unresolved_pairs=1,
        improved=1,
        regressed=1,
        both_pass=1,
        both_fail=0,
        net_uplift=0,
    )
    assert report.metrics[QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE].value == 0.75
    assert report.metrics[QualityMetricName.NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE].denominator == 3
    assert report.source_deliveries == report.completed_source_deliveries == 1
    assert sum(row.observed_calls for row in report.provider_calls) == 10
    assert (
        sum(
            row.observed_calls
            for row in report.provider_calls
            if row.stage is ProviderStage.EXTRACTION
        )
        == 1
    )
    assert (
        next(row for row in report.provider_calls if row.stage is ProviderStage.EXTRACTION).workload
        == "shared_source"
    )
    assert report.missing_source_measurements == report.missing_case_measurements == 0
    store.write_benchmark_report(report)
    assert (store.root / "benchmark-report.json").exists()


def test_qa_only_report_keeps_full_suite_sources_and_pipeline_calls_without_component_scores(
    tmp_path,
):
    preparation, _ = _prepare_run()
    assert len(preparation.selected_cases) == 209
    store = create_or_resume_store(tmp_path / "qa-run", preparation)
    source_cases = {}
    for case in preparation.selected_cases:
        source_cases.setdefault(case.case_id.split(":")[0], case)
        measured = MeasurementRecorder()
        with measured.bind():
            for arm in ("no_ltm", "with_ltm"):
                with measurement_arm(arm):
                    if arm == "with_ltm":
                        record_provider_call(
                            ProviderStage.EMBEDDING, basis="sdk_invocation", outcome="success"
                        )
                    for stage, basis in (
                        (ProviderStage.REWRITE, "http_request"),
                        (ProviderStage.KIRA, "kira_chat"),
                        (ProviderStage.JUDGE, "http_request"),
                    ):
                        record_provider_call(stage, basis=basis, outcome="success")
        output = _result(
            case.case_id, JudgeVerdict.FAIL, JudgeVerdict.PASS, score_tasks=True
        ).model_dump(mode="json")
        store.append_case_attempt(
            CaseAttemptArtifact(
                case_id=case.case_id,
                suite=Suite.CROSS_SESSION,
                attempt=1,
                completed_at=datetime.now(UTC),
                outcome=Outcome.REVIEW_REQUIRED,
                output=output,
                output_sha256=output_sha256(output),
                measurement=measured.snapshot(),
            )
        )
    for bundle_id, case in source_cases.items():
        source_events = len(case.inputs.session_a_messages) // 2
        conversation_id = uuid4()
        source_measure = MeasurementRecorder()
        with source_measure.bind():
            for _ in range(source_events):
                for stage in (ProviderStage.EXTRACTION, ProviderStage.EMBEDDING):
                    record_provider_call(stage, basis="sdk_invocation", outcome="success")
        store.write_bundle_source(
            BundleSourceArtifact(
                identity=preparation.identity,
                bundle_id=bundle_id,
                logical_user_id=case.inputs.user_id,
                source_sha256="c" * 64,
                persisted_user_id=f"source-user-{bundle_id}",
                source_session_id=f"source-conversation-{bundle_id}",
                source_conversation_id=conversation_id,
                expected_source_events=source_events,
                events=tuple(
                    BundleSourceEventArtifact(
                        event_id=uuid4(),
                        user_id=f"source-user-{bundle_id}",
                        session_id=f"source-conversation-{bundle_id}",
                        conversation_id=conversation_id,
                        turn_id=f"source-turn-{index}",
                        boundary_message_id=index * 2,
                        completed=True,
                    )
                    for index in range(1, source_events + 1)
                ),
                ready=True,
                measurement=source_measure.snapshot(),
            )
        )
    report = build_benchmark_report(run_root=store.root, dataset_root=default_dataset_root())
    assert report.evaluation_scope == "selected_suites"
    assert report.selected_suites == (Suite.CROSS_SESSION,)
    assert report.selected_suite_coverage == "full_selected_suites"
    assert report.selected_cases == report.attempted_cases == report.total_attempts == 209
    assert report.suite_outcomes == {Suite.CROSS_SESSION: {"REVIEW_REQUIRED": 209}}
    unrun_metrics = (
        QualityMetricName.FORMATION_PRECISION,
        QualityMetricName.FORMATION_RECALL,
        QualityMetricName.FORMATION_F1,
        QualityMetricName.RETRIEVAL_RECALL_AT_3,
        QualityMetricName.RETRIEVAL_MRR_AT_10,
        QualityMetricName.REWRITE_CONSTRAINT_PASS_RATE,
        QualityMetricName.REWRITE_SEMANTIC_PASS_RATE,
    )
    assert len(report.metrics) == 11
    for name in unrun_metrics:
        assert report.metrics[name].model_dump() == {"value": None, "denominator": 0}
        assert report.diagnostic_metrics[name].model_dump() == {"value": None, "denominator": 0}
    accepted = sum(is_acceptance_case(case) for case in preparation.selected_cases)
    for name, value in (
        (QualityMetricName.NO_LTM_FINAL_QA_SEMANTIC_PASS_RATE, 0),
        (QualityMetricName.NO_LTM_TASK_SUCCESS_RATE, 0),
        (QualityMetricName.FINAL_QA_SEMANTIC_PASS_RATE, 1),
        (QualityMetricName.FINAL_QA_TASK_SUCCESS_RATE, 1),
    ):
        assert report.metrics[name].value == value
        assert report.metrics[name].denominator == accepted
    assert report.memory_effect.total_pairs == report.memory_effect.improved == accepted
    assert report.memory_effect.resolved_pairs == accepted
    assert report.memory_effect.net_uplift == 1
    assert report.source_bundles == report.source_bundles_ready == 4
    assert report.source_deliveries == report.completed_source_deliveries == 253
    assert report.missing_source_measurements == report.missing_case_measurements == 0
    groups = {
        (row.workload, row.stage, row.arm, row.basis): row.observed_calls
        for row in report.provider_calls
    }
    assert groups == {
        ("shared_source", ProviderStage.EXTRACTION, None, "sdk_invocation"): 253,
        ("shared_source", ProviderStage.EMBEDDING, None, "sdk_invocation"): 253,
        ("cross_session", ProviderStage.EMBEDDING, "with_ltm", "sdk_invocation"): 209,
        **{
            ("cross_session", stage, arm, basis): 209
            for arm in ("no_ltm", "with_ltm")
            for stage, basis in (
                (ProviderStage.REWRITE, "http_request"),
                (ProviderStage.KIRA, "kira_chat"),
                (ProviderStage.JUDGE, "http_request"),
            )
        },
    }
    # A prior report is a derived artifact, never quality evidence for this rebuild.
    store.write_benchmark_report(
        report.model_copy(
            update={
                "metrics": {
                    **report.metrics,
                    **{name: QualityMetric(value=1, denominator=10) for name in unrun_metrics},
                }
            }
        )
    )
    rebuilt = build_benchmark_report(run_root=store.root, dataset_root=default_dataset_root())
    assert rebuilt == report


@pytest.mark.parametrize(
    ("suites", "scope"),
    (
        (tuple(Suite), "full_corpus"),
        ((Suite.REWRITE, Suite.FORMATION), "selected_suites"),
    ),
)
def test_report_scope_describes_selection_before_cases_are_attempted(tmp_path, suites, scope):
    preparation, _ = _prepare_run(suites)
    store = create_or_resume_store(tmp_path / "run", preparation)
    report = build_benchmark_report(run_root=store.root, dataset_root=default_dataset_root())
    assert report.evaluation_scope == scope
    assert report.selected_suites == tuple(suite for suite in Suite if suite in suites)
    assert report.selected_suite_coverage == "full_selected_suites"
    assert report.attempted_cases == 0
    assert all(
        metric.value is None and metric.denominator == 0 for metric in report.metrics.values()
    )


def test_partial_usage_totals_are_na_with_observed_subtotals():
    measured = MeasurementRecorder()
    with measured.bind():
        record_provider_call(
            ProviderStage.REWRITE,
            basis="http_request",
            outcome="success",
            usage={"input": 10, "output": 2, "total": 12},
        )
        record_provider_call(ProviderStage.REWRITE, basis="http_request", outcome="success")
    rows, _ = _operations({"rewrite": [measured.snapshot()]})
    assert rows[0].observed_calls == 2
    assert rows[0].calls_reporting_usage == 1
    assert rows[0].total_tokens is None
    assert rows[0].observed_total_tokens == 12
