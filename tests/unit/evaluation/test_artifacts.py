"""Local artifact layout, binding and fail-closed resume tests."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

import evaluation.artifacts as artifacts
from evaluation.artifacts import (
    ArtifactRunIdentity,
    ArtifactStore,
    AuditBatchArtifact,
    AuditDecisionArtifact,
    BenchmarkSummaryArtifact,
    BundleSourceArtifact,
    BundleSourceEventArtifact,
    CaseAttemptArtifact,
    DiagnosticArtifact,
    MetricArtifact,
    SemanticJudgmentArtifact,
    SuiteSummaryArtifact,
)
from evaluation.audit import (
    AuditCandidate,
    AuditDisposition,
    AuditPolicy,
    HumanAuditDecision,
    select_audit_batch,
)
from evaluation.isolation import (
    allocate_case_resources,
    create_isolation_plan,
    isolation_plan_sha256,
    new_isolation_ledger,
)
from evaluation.models import (
    BenchmarkVariant,
    GitSource,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.scoring import (
    JudgeProvenance,
    JudgeVerdict,
    SemanticJudgment,
    output_sha256,
)

_NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_HASH = "a" * 64


def _identity(
    *,
    config_sha256: str = "d" * 64,
    isolation_sha256: str | None = None,
) -> ArtifactRunIdentity:
    source = GitSource(sha="1" * 40, dirty=False)
    provenance = RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": _HASH, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": "2.0.20+viettel.6",
        },
    )
    return ArtifactRunIdentity(
        run_id=_RUN_ID,
        profile=Profile.INTERNAL_TEST,
        variant=BenchmarkVariant.WORKING_TREE,
        provenance=provenance,
        dataset_id="kira-ltm-v1",
        dataset_version="1.0.0-test",
        dataset_sha256="b" * 64,
        compilation_sha256="c" * 64,
        config_sha256=config_sha256,
        isolation_sha256=isolation_sha256,
        seed=742,
        suites=(Suite.REWRITE,),
        selected_case_ids=("conv01:case-1", "conv01:case-2", "conv01:case-3"),
    )


def _attempt(
    case_id: str,
    outcome: Outcome,
    *,
    attempt: int = 1,
    output: object | None = None,
) -> CaseAttemptArtifact:
    return CaseAttemptArtifact(
        case_id=case_id,
        suite=Suite.REWRITE,
        attempt=attempt,
        completed_at=_NOW,
        outcome=outcome,
        output=output,
        output_sha256=output_sha256(output) if output is not None else None,
        reason_codes=("provider_unavailable",) if outcome is Outcome.DEPENDENCY_ERROR else (),
    )


def _judge() -> JudgeProvenance:
    return JudgeProvenance(
        provider="internal-vllm",
        model="judge-model",
        prompt_sha256="e" * 64,
        response_schema_sha256="f" * 64,
    )


def _judgment(attempt: CaseAttemptArtifact, verdict: JudgeVerdict) -> SemanticJudgmentArtifact:
    assert attempt.output_sha256 is not None
    return SemanticJudgmentArtifact(
        judgment=SemanticJudgment(
            case_id=attempt.case_id,
            suite=Suite.REWRITE,
            output_sha256=attempt.output_sha256,
            verdict=verdict,
            reason_code="semantic_equivalent",
            rationale="The output preserves the expected intent and constraints.",
            judge=_judge(),
        )
    )


def _audit_batch(attempt: CaseAttemptArtifact, verdict: JudgeVerdict) -> AuditBatchArtifact:
    assert attempt.output_sha256 is not None
    candidate = AuditCandidate(
        case_id=attempt.case_id,
        bundle_id="conv01",
        suite=Suite.REWRITE,
        variant=BenchmarkVariant.WORKING_TREE,
        output_sha256=attempt.output_sha256,
        judge_verdict=verdict,
        judge_reason_code="semantic_equivalent",
    )
    return AuditBatchArtifact(
        batch_id="audit-1",
        batch=select_audit_batch(
            [candidate],
            policy=AuditPolicy(seed=742, sample_rate=1.0),
        ),
    )


def _audit_decision(attempt: CaseAttemptArtifact) -> AuditDecisionArtifact:
    assert attempt.output_sha256 is not None
    return AuditDecisionArtifact(
        batch_id="audit-1",
        decision=HumanAuditDecision(
            case_id=attempt.case_id,
            output_sha256=attempt.output_sha256,
            reviewer="mentor",
            reviewed_at=_NOW,
            human_verdict=JudgeVerdict.PASS,
            reason_code="agrees_with_judge",
            notes="Checked the frozen gold and synthetic output.",
        ),
    )


def test_create_has_minimal_layout_and_never_overwrites_existing_run(tmp_path: Path):
    root = tmp_path / "run"
    store = ArtifactStore.create(root, identity=_identity(), created_at=_NOW)

    assert store.root == root.resolve()
    assert sorted(path.name for path in root.iterdir()) == [
        "audits.jsonl",
        "cases.jsonl",
        "diagnostics",
        "judgments.jsonl",
        "manifest.json",
    ]
    assert store.resume_plan().run_case_ids == _identity().selected_case_ids
    with pytest.raises(FileExistsError):
        ArtifactStore.create(root, identity=_identity(), created_at=_NOW)


def test_resume_requires_exact_non_secret_run_identity(tmp_path: Path):
    root = tmp_path / "run"
    ArtifactStore.create(root, identity=_identity(), created_at=_NOW)

    resumed = ArtifactStore.resume(root, expected_identity=_identity())
    assert resumed.manifest.identity.config_sha256 == "d" * 64
    with pytest.raises(ValueError, match="identity"):
        ArtifactStore.resume(root, expected_identity=_identity(config_sha256="9" * 64))


def test_attempts_are_contiguous_bound_to_selected_cases_and_hash_checked(tmp_path: Path):
    store = ArtifactStore.create(tmp_path / "run", identity=_identity(), created_at=_NOW)
    first = _attempt("conv01:case-1", Outcome.PASS, output={"rewrite": "Hà Nội"})
    store.append_case_attempt(first)
    store.append_case_attempt(
        _attempt("conv01:case-1", Outcome.FAIL, attempt=2, output={"rewrite": "Đà Nẵng"})
    )

    with pytest.raises(ValueError, match="contiguous"):
        store.append_case_attempt(
            _attempt("conv01:case-1", Outcome.FAIL, attempt=4, output={"rewrite": "Sai"})
        )
    with pytest.raises(ValueError, match="outside"):
        store.append_case_attempt(_attempt("conv99:case-1", Outcome.PASS, output="x"))
    with pytest.raises(ValidationError, match="hash"):
        CaseAttemptArtifact(
            case_id="conv01:case-1",
            suite=Suite.REWRITE,
            attempt=1,
            completed_at=_NOW,
            outcome=Outcome.PASS,
            output="x",
            output_sha256="0" * 64,
        )

    resumed = ArtifactStore.resume(store.root, expected_identity=_identity())
    assert resumed.latest_outcome_counts == {Outcome.FAIL: 1}


def test_error_attempts_store_codes_not_raw_provider_payloads():
    error = _attempt("conv01:case-1", Outcome.DEPENDENCY_ERROR)
    assert error.output is None
    assert error.reason_codes == ("provider_unavailable",)
    with pytest.raises(ValidationError, match="raw provider output"):
        _attempt("conv01:case-1", Outcome.DEPENDENCY_ERROR, output={"secret": "raw"})


def test_judgment_and_audit_are_bound_to_persisted_output(tmp_path: Path):
    store = ArtifactStore.create(tmp_path / "run", identity=_identity(), created_at=_NOW)
    attempt = _attempt(
        "conv01:case-3",
        Outcome.REVIEW_REQUIRED,
        output={"rewrite": "So sánh FTTH tại Hà Nội"},
    )
    store.append_case_attempt(attempt)
    judgment = _judgment(attempt, JudgeVerdict.PASS)
    store.append_judgment(judgment)
    batch = _audit_batch(attempt, JudgeVerdict.PASS)
    store.append_audit_batch(batch)

    assert store.resume_plan().pending_audit_case_ids == (attempt.case_id,)
    store.append_audit_decision(_audit_decision(attempt))
    assert store.resume_plan().ready_case_ids == (attempt.case_id,)

    stale = judgment.model_copy(
        update={"judgment": judgment.judgment.model_copy(update={"output_sha256": "0" * 64})}
    )
    with pytest.raises(ValueError, match="persisted case output"):
        store.append_judgment(stale)


def test_resume_plan_separates_execution_judgment_audit_ready_and_attention(tmp_path: Path):
    store = ArtifactStore.create(tmp_path / "run", identity=_identity(), created_at=_NOW)
    store.append_case_attempt(_attempt("conv01:case-1", Outcome.PASS, output="deterministic"))
    store.append_case_attempt(_attempt("conv01:case-2", Outcome.DEPENDENCY_ERROR))
    review = _attempt("conv01:case-3", Outcome.REVIEW_REQUIRED, output="semantic")
    store.append_case_attempt(review)

    plan = store.resume_plan()
    assert plan.run_case_ids == ("conv01:case-2",)
    assert plan.pending_judgment_case_ids == ("conv01:case-3",)
    assert plan.ready_case_ids == ("conv01:case-1",)

    store.append_judgment(_judgment(review, JudgeVerdict.UNCERTAIN))
    plan = store.resume_plan()
    assert plan.pending_judgment_case_ids == ()
    assert plan.pending_audit_case_ids == ("conv01:case-3",)


def test_gold_error_is_attention_not_a_completed_quality_case(tmp_path: Path):
    store = ArtifactStore.create(tmp_path / "run", identity=_identity(), created_at=_NOW)
    attempt = _attempt("conv01:case-3", Outcome.REVIEW_REQUIRED, output="semantic")
    store.append_case_attempt(attempt)
    store.append_judgment(_judgment(attempt, JudgeVerdict.PASS))
    store.append_audit_batch(_audit_batch(attempt, JudgeVerdict.PASS))
    decision = _audit_decision(attempt)
    gold_error = decision.model_copy(
        update={
            "decision": decision.decision.model_copy(
                update={
                    "disposition": AuditDisposition.GOLD_ERROR,
                    "human_verdict": None,
                    "reason_code": "gold_is_wrong",
                }
            )
        }
    )
    store.append_audit_decision(gold_error)

    plan = store.resume_plan()
    assert plan.attention_case_ids == (attempt.case_id,)
    assert plan.ready_case_ids == ()


def test_corrupt_or_partial_jsonl_fails_closed_on_resume(tmp_path: Path):
    root = tmp_path / "run"
    ArtifactStore.create(root, identity=_identity(), created_at=_NOW)
    (root / "cases.jsonl").write_text('{"partial":', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSONL"):
        ArtifactStore.resume(root, expected_identity=_identity())


def test_summary_report_and_safe_diagnostic_are_replaceable_derived_artifacts(tmp_path: Path):
    store = ArtifactStore.create(tmp_path / "run", identity=_identity(), created_at=_NOW)
    summary = BenchmarkSummaryArtifact(
        run_id=_RUN_ID,
        suites=(
            SuiteSummaryArtifact(
                suite=Suite.REWRITE,
                eligible=3,
                attempted=1,
                scored=1,
                outcomes={Outcome.PASS: 1},
                metrics=(
                    MetricArtifact(
                        name="constraint_pass_rate",
                        value=1.0,
                        numerator=1,
                        denominator=1,
                    ),
                ),
            ),
        ),
        safety_passed=True,
        pending_judgment=0,
        pending_audit=0,
    )
    store.write_summary(summary)
    store.write_report("# First report")
    store.write_report("# Regenerated report")
    diagnostic = DiagnosticArtifact(
        case_id="conv01:case-2",
        attempt=1,
        outcome=Outcome.DEPENDENCY_ERROR,
        reason_codes=("provider_unavailable",),
        trace_id="abc123",
    )
    path = store.write_diagnostic(diagnostic)

    assert (store.root / "summary.json").is_file()
    assert (store.root / "report.md").read_text(encoding="utf-8") == "# Regenerated report\n"
    assert path.is_file()
    assert ":" not in path.name
    with pytest.raises(FileExistsError):
        store.write_diagnostic(diagnostic)


@pytest.mark.parametrize("winerror", [5, 32])
def test_artifact_replace_retries_transient_windows_lock(tmp_path: Path, monkeypatch, winerror):
    path = tmp_path / "isolation-ledger.json"
    path.write_text("before", encoding="utf-8")
    error = PermissionError("locked")
    error.winerror = winerror
    calls = []
    delays = []
    real_replace = artifacts.os.replace

    def locked_replace(source, target):
        calls.append((source, target))
        if len(calls) <= 2:
            raise error
        real_replace(source, target)

    monkeypatch.setattr(artifacts.sys, "platform", "win32")
    monkeypatch.setattr(artifacts.os, "replace", locked_replace)
    monkeypatch.setattr(artifacts.time, "sleep", delays.append)

    artifacts._replace(path, "after")

    assert path.read_text(encoding="utf-8") == "after"
    assert len(calls) == 3
    assert all(call == calls[0] for call in calls)
    assert delays == [0.01, 0.03]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("winerror", [5, 32])
def test_artifact_replace_raises_after_bounded_windows_lock_retries(
    tmp_path: Path, monkeypatch, winerror
):
    path = tmp_path / "isolation-ledger.json"
    path.write_text("before", encoding="utf-8")
    error = PermissionError("locked")
    error.winerror = winerror
    calls = []
    delays = []

    def locked_replace(source, target):
        calls.append((source, target))
        raise error

    monkeypatch.setattr(artifacts.sys, "platform", "win32")
    monkeypatch.setattr(artifacts.os, "replace", locked_replace)
    monkeypatch.setattr(artifacts.time, "sleep", delays.append)

    with pytest.raises(PermissionError) as caught:
        artifacts._replace(path, "after")

    assert caught.value is error
    assert len(calls) == 4
    assert all(call == calls[0] for call in calls)
    assert delays == [0.01, 0.03, 0.1]
    assert path.read_text(encoding="utf-8") == "before"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("platform", "error_type", "winerror"),
    [
        ("linux", PermissionError, 5),
        ("win32", PermissionError, None),
        ("win32", PermissionError, 13),
        ("win32", OSError, 5),
    ],
)
def test_artifact_replace_does_not_retry_other_errors(
    tmp_path: Path, monkeypatch, platform, error_type, winerror
):
    path = tmp_path / "isolation-ledger.json"
    path.write_text("before", encoding="utf-8")
    error = error_type("cannot replace")
    if winerror is not None:
        error.winerror = winerror
    calls = []
    delays = []

    def failed_replace(source, target):
        calls.append((source, target))
        raise error

    monkeypatch.setattr(artifacts.sys, "platform", platform)
    monkeypatch.setattr(artifacts.os, "replace", failed_replace)
    monkeypatch.setattr(artifacts.time, "sleep", delays.append)

    with pytest.raises(error_type) as caught:
        artifacts._replace(path, "after")

    assert caught.value is error
    assert len(calls) == 1
    assert delays == []
    assert path.read_text(encoding="utf-8") == "before"
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_summary_denominators_and_safety_are_rejected():
    with pytest.raises(ValidationError):
        MetricArtifact(name="recall_at_3", value=0.0, numerator=0, denominator=0)
    with pytest.raises(ValidationError):
        BenchmarkSummaryArtifact(
            run_id=_RUN_ID,
            suites=(),
            safety_passed=True,
            safety_violation_codes=("cross_user_leak",),
            pending_judgment=0,
            pending_audit=0,
        )


def test_isolation_ledger_is_atomically_bound_to_artifact_run(tmp_path: Path):
    plan = create_isolation_plan(
        run_id=_RUN_ID,
        owner_token=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        conversation_database_url="postgresql://eval:secret@localhost/eval",
        memory_database_url="postgresql://eval:secret@localhost/eval",
    )
    plan_hash = isolation_plan_sha256(plan)
    store = ArtifactStore.create(
        tmp_path / "run",
        identity=_identity(isolation_sha256=plan_hash),
        created_at=_NOW,
    )
    ledger = new_isolation_ledger(plan)

    store.write_isolation_plan(plan)
    path = store.write_isolation_ledger(ledger)

    assert path.name == "isolation-ledger.json"
    assert store.load_isolation_ledger() == ledger
    expanded = ledger.model_copy(
        update={"resources": (allocate_case_resources(plan, case_id="conv01:case-1", attempt=1),)}
    )
    store.write_isolation_ledger(expanded)
    claimed_resource = expanded.resources[0].model_copy(
        update={"conversation_id": UUID(int=50), "event_id": UUID(int=51)}
    )
    claimed = expanded.model_copy(update={"resources": (claimed_resource,)})
    store.write_isolation_ledger(claimed)
    reassigned = claimed.model_copy(
        update={"resources": (claimed_resource.model_copy(update={"event_id": UUID(int=52)}),)}
    )
    with pytest.raises(ValueError, match="cannot be removed or reassigned"):
        store.write_isolation_ledger(reassigned)
    with pytest.raises(ValueError, match="cannot be removed"):
        store.write_isolation_ledger(ledger)
    wrong = ledger.model_copy(update={"plan_sha256": "0" * 64})
    with pytest.raises(ValueError, match="another artifact run"):
        store.write_isolation_ledger(wrong)


def test_isolation_plan_is_immutable_and_hash_bound(tmp_path: Path):
    plan = create_isolation_plan(
        run_id=_RUN_ID,
        owner_token=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        conversation_database_url="postgresql://eval:secret@localhost/eval",
        memory_database_url="postgresql://eval:secret@localhost/eval",
    )
    store = ArtifactStore.create(
        tmp_path / "run",
        identity=_identity(isolation_sha256=isolation_plan_sha256(plan)),
        created_at=_NOW,
    )

    path = store.write_isolation_plan(plan)
    assert path.name == "isolation-plan.json"
    assert store.load_isolation_plan() == plan
    assert store.write_isolation_plan(plan) == path

    other = plan.model_copy(update={"owner_token": UUID(int=999)})
    with pytest.raises(ValueError, match="does not match"):
        store.write_isolation_plan(other)


def _bundle_source(identity: ArtifactRunIdentity) -> BundleSourceArtifact:
    return BundleSourceArtifact(
        identity=identity,
        bundle_id="conv01",
        logical_user_id="user01",
        source_sha256="f" * 64,
        persisted_user_id="eval:source-user",
        source_session_id="eval:source-session",
        expected_source_events=2,
    )


def _bundle_event(index: int, *, completed: bool = False) -> BundleSourceEventArtifact:
    return BundleSourceEventArtifact(
        event_id=UUID(int=index),
        user_id="eval:source-user",
        session_id="eval:source-session",
        conversation_id=UUID(int=100),
        turn_id=f"turn-{index}",
        boundary_message_id=index * 2,
        source_timestamp=_NOW,
        completed=completed,
        memory_ids=(UUID(int=200 + index),) if completed else (),
    )


def test_bundle_source_progress_survives_resume_and_rejects_reformation_or_reassignment(tmp_path):
    identity = _identity()
    store = ArtifactStore.create(tmp_path / "run", identity=identity, created_at=_NOW)
    initial = _bundle_source(identity)
    path = store.write_bundle_source(initial)
    pending = initial.model_copy(
        update={"source_conversation_id": UUID(int=100), "events": (_bundle_event(1),)}
    )
    store.write_bundle_source(pending)
    completed = pending.model_copy(update={"events": (_bundle_event(1, completed=True),)})
    store.write_bundle_source(completed)
    resumed = ArtifactStore.resume(store.root, expected_identity=identity)
    assert resumed.load_bundle_source("conv01") == completed
    assert path.parent.name == "bundles"
    with pytest.raises(ValueError, match="cannot be removed"):
        resumed.write_bundle_source(completed.model_copy(update={"events": ()}))
    with pytest.raises(ValueError, match="cannot be replaced"):
        resumed.write_bundle_source(completed.model_copy(update={"source_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="cannot be reassigned"):
        resumed.write_bundle_source(
            completed.model_copy(
                update={
                    "source_conversation_id": UUID(int=101),
                    "events": (
                        _bundle_event(1, completed=True).model_copy(
                            update={"conversation_id": UUID(int=101)}
                        ),
                    ),
                }
            )
        )
    with pytest.raises(ValueError, match="completed source"):
        resumed.write_bundle_source(pending)
    ready = completed.model_copy(
        update={
            "events": (_bundle_event(1, completed=True), _bundle_event(2, completed=True)),
            "ready": True,
            "memory_gold_ids": {UUID(int=201): ("conv01:M1",)},
        }
    )
    resumed.write_bundle_source(ready)
    assert resumed.load_bundle_source("conv01").ready
    with pytest.raises(ValueError, match="cannot become incomplete"):
        resumed.write_bundle_source(ready.model_copy(update={"ready": False}))
    with pytest.raises(ValueError, match="gold mapping cannot be removed"):
        resumed.write_bundle_source(ready.model_copy(update={"memory_gold_ids": {}}))
    corrupt = ready.model_copy(
        update={"ready": False, "failed": True, "failure_code": "source_receipt_missing"}
    )
    resumed.write_bundle_source(corrupt)
    with pytest.raises(ValueError, match="terminal"):
        resumed.write_bundle_source(ready)


def test_failed_bundle_source_is_terminal_and_cannot_be_retried_by_another_qa(tmp_path):
    identity = _identity()
    store = ArtifactStore.create(tmp_path / "run", identity=identity, created_at=_NOW)
    initial = _bundle_source(identity)
    store.write_bundle_source(initial)
    failed = initial.model_copy(update={"failed": True, "failure_code": "source_job_dead"})
    store.write_bundle_source(failed)
    assert store.load_bundle_source("conv01").failed
    with pytest.raises(ValueError, match="terminal"):
        store.write_bundle_source(initial)
    with pytest.raises(ValueError, match="every source event"):
        store.write_bundle_source(failed.model_copy(update={"ready": True}))


@pytest.mark.parametrize("mutation", ["owner", "session", "conversation", "order", "ready"])
def test_bundle_source_rejects_cross_owner_and_incomplete_or_reordered_evidence(mutation):
    initial = _bundle_source(_identity())
    event = _bundle_event(1)
    data = initial.model_dump()
    data.update(source_conversation_id=UUID(int=100), events=(event.model_dump(),))
    if mutation == "owner":
        data["events"][0]["user_id"] = "another-user"
    elif mutation == "session":
        data["events"][0]["session_id"] = "another-session"
    elif mutation == "conversation":
        data["events"][0]["conversation_id"] = UUID(int=101)
    elif mutation == "order":
        data["events"] = (_bundle_event(2).model_dump(), event.model_dump())
    else:
        data["ready"] = True
    with pytest.raises(ValueError):
        BundleSourceArtifact.model_validate(data)
