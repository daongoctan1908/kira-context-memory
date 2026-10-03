"""Offline-safe benchmark CLI plus dependency preflight."""

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values
from pydantic import TypeAdapter

from app.config.settings import Settings
from evaluation.artifacts import ArtifactRunManifest, ArtifactStore
from evaluation.audit import (
    AuditBatch,
    AuditCandidate,
    AuditPolicy,
    AuditReconciliation,
    HumanAuditDecision,
    reconcile_audits,
    select_audit_batch,
)
from evaluation.audit_candidates import build_audit_candidates
from evaluation.compiler import compilation_json_bytes, compile_dataset
from evaluation.config import load_config
from evaluation.cross_session import CrossSessionEvaluator
from evaluation.dataset import default_dataset_root
from evaluation.isolation import (
    IsolationPlan,
    apply_isolation,
    create_isolation_plan,
    isolation_plan_sha256,
    new_isolation_ledger,
)
from evaluation.models import Outcome, PerformanceReviewVerdict, Profile, RunProvenance, Suite
from evaluation.native_executor import (
    NativeBenchmarkExecutor,
    NativeFormationEvaluator,
    NativeRetrievalEvaluator,
)
from evaluation.native_runtime import (
    cleanup_native_runtime,
    create_native_runtime,
    reconcile_interrupted_attempts,
)
from evaluation.preflight import run_preflight
from evaluation.provenance import capture_local_provenance
from evaluation.release_evidence import (
    CleanupEvidence,
    ConfirmationComponent,
    ExactImageSet,
    OfficialConfirmationReport,
    PerformanceEvidence,
    PerformanceReview,
    ReleaseDecision,
    RunQualityEvidence,
    build_confirmation_report,
    build_performance_review,
    build_promotion_report,
    build_run_quality_evidence,
)
from evaluation.rewrite import RewriteEvaluator
from evaluation.runner import (
    MockBenchmarkExecutor,
    benchmark_execution_complete,
    create_or_resume_store,
    execute_benchmark_cases,
    prepare_benchmark_run,
)
from scripts.benchmark.validate_dataset import validate_dataset

_AUDIT_CANDIDATES = TypeAdapter(list[AuditCandidate])
_AUDIT_DECISIONS = TypeAdapter(list[HumanAuditDecision])
_NATIVE_SUITES = tuple(Suite)


def _new_output(path: Path, contents: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "xb" if isinstance(contents, bytes) else "x"
    kwargs = {} if isinstance(contents, bytes) else {"encoding": "utf-8", "newline": "\n"}
    with path.open(mode, **kwargs) as stream:
        stream.write(contents)


def _load_jsonl(path: Path, adapter: TypeAdapter) -> list:
    values = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith("\n") or not line.strip():
                raise ValueError(f"invalid JSONL record at line {line_number}")
            values.append(json.loads(line))
    return adapter.validate_python(values)


def _load_application_settings(
    env_file: Path | None,
    *,
    file_only: bool,
) -> Settings:
    """Load the app adapter settings with the same explicit source policy as eval config."""

    if not file_only:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    if env_file is None or not env_file.is_file():
        raise ValueError("file-only mode needs an explicit file")
    values = dotenv_values(env_file, interpolate=False, encoding="utf-8")
    fields = Settings.model_fields
    selected = {
        key.lower(): value
        for key, value in values.items()
        if value is not None and key.lower() in fields
    }
    return Settings(_env_file=None, **selected)  # type: ignore[arg-type]


def _require_native_run_contract(
    profile: Profile,
    suites: tuple[Suite, ...],
    formation_mode: str,
) -> tuple[Suite, ...]:
    if profile not in {Profile.PC_OPENAI_ACCEPTANCE, Profile.INTERNAL_TEST}:
        raise ValueError("canonical native runner requires a PC or internal profile")
    if len(suites) != len(_NATIVE_SUITES) or set(suites) != set(_NATIVE_SUITES):
        raise ValueError("native runner requires each canonical suite exactly once")
    if formation_mode != "persistent":
        raise ValueError("native runner requires persistent formation")
    return _NATIVE_SUITES


async def _execute_native_run(
    *,
    config,
    base_settings: Settings,
    plan: IsolationPlan,
    preparation,
    store: ArtifactStore,
) -> bool:
    """Compose the exact adapters, execute sequentially, and clean only owned state."""

    reconcile_interrupted_attempts(store, preparation.selected_cases)
    ledger = store.load_isolation_ledger()
    runtime = await create_native_runtime(
        config=config,
        base_settings=base_settings,
        plan=plan,
        ledger=ledger,
        artifacts=store,
        cases=preparation.selected_cases,
    )
    complete = False
    try:
        executor = NativeBenchmarkExecutor(
            {
                Suite.FORMATION: NativeFormationEvaluator(runtime.formation, runtime.judge),
                Suite.RETRIEVAL: NativeRetrievalEvaluator(runtime.retrieval),
                Suite.REWRITE: RewriteEvaluator(
                    runtime.rewriter,
                    runtime.judge,
                    profile=config.profile,
                    backend="native",
                ),
                Suite.CROSS_SESSION: CrossSessionEvaluator(
                    runtime.cross_session,
                    runtime.judge,
                    profile=config.profile,
                    backend="native",
                    readiness_timeout_seconds=config.total_timeout_seconds,
                ),
            }
        )
        await execute_benchmark_cases(preparation, store, executor)
        complete = benchmark_execution_complete(preparation, store)
        if complete:
            await runtime.retrieval.cleanup()
    finally:
        await runtime.aclose()
    if complete:
        await cleanup_native_runtime(
            config=config,
            plan=plan,
            ledger=store.load_isolation_ledger(),
        )
    return complete


def _add_offline_commands(commands: argparse._SubParsersAction) -> None:
    validate = commands.add_parser("validate", help="Validate the canonical dataset offline")
    validate.add_argument("--root", type=Path, default=default_dataset_root())
    validate.add_argument("--output", type=Path)

    compile_command = commands.add_parser(
        "compile", help="Compile deterministic benchmark cases offline"
    )
    compile_command.add_argument("--root", type=Path, default=default_dataset_root())
    compile_command.add_argument("--seed", type=int, default=742)
    compile_command.add_argument("--output", type=Path, required=True)

    resume = commands.add_parser("resume", help="Inspect a run and print its safe resume plan")
    resume.add_argument("--run-root", type=Path, required=True)
    resume.add_argument("--output", type=Path)

    audit = commands.add_parser("audit", help="Export or import targeted human audit records")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    candidates = audit_commands.add_parser(
        "candidates", help="Derive content-free semantic audit candidates from one native run"
    )
    candidates.add_argument("--run-root", type=Path, required=True)
    candidates.add_argument("--dataset-root", type=Path, default=default_dataset_root())
    candidates.add_argument("--output", type=Path, required=True)
    export = audit_commands.add_parser("export", help="Select the deterministic audit batch")
    export.add_argument("--candidates", type=Path, required=True)
    export.add_argument("--seed", type=int, default=742)
    export.add_argument("--sample-rate", type=float, default=0.10)
    export.add_argument("--output", type=Path, required=True)
    import_command = audit_commands.add_parser(
        "import", help="Validate human decisions and reconcile semantic verdicts"
    )
    import_command.add_argument("--candidates", type=Path, required=True)
    import_command.add_argument("--batch", type=Path, required=True)
    import_command.add_argument("--decisions", type=Path, required=True)
    import_command.add_argument("--output", type=Path, required=True)

    release = commands.add_parser(
        "release", help="Build offline official confirmation and release evidence"
    )
    release_commands = release.add_subparsers(dest="release_command", required=True)
    scorecard = release_commands.add_parser(
        "scorecard", help="Derive one internal run quality scorecard"
    )
    scorecard.add_argument("--run-root", type=Path, required=True)
    scorecard.add_argument("--dataset-root", type=Path, default=default_dataset_root())
    scorecard.add_argument("--audit-reconciliation", type=Path, required=True)
    scorecard.add_argument("--output", type=Path, required=True)
    confirm = release_commands.add_parser(
        "confirm", help="Aggregate exactly three paired control/candidate scorecards"
    )
    confirm.add_argument("--component", choices=list(ConfirmationComponent), required=True)
    confirm.add_argument("--control", type=Path, action="append", required=True)
    confirm.add_argument("--candidate", type=Path, action="append", required=True)
    confirm.add_argument("--output", type=Path, required=True)
    performance = release_commands.add_parser(
        "performance", help="Review bounded post-quality performance evidence"
    )
    performance.add_argument("--confirmation", type=Path, required=True)
    performance.add_argument("--evidence", type=Path, required=True)
    performance.add_argument("--verdict", choices=list(PerformanceReviewVerdict), required=True)
    performance.add_argument("--reviewer", required=True)
    performance.add_argument("--rationale", required=True)
    performance.add_argument("--output", type=Path, required=True)
    promote = release_commands.add_parser(
        "promote", help="Bind quality, performance, image and cleanup evidence"
    )
    promote.add_argument("--confirmation", type=Path, required=True)
    promote.add_argument("--performance", type=Path, required=True)
    promote.add_argument("--images", type=Path, required=True)
    promote.add_argument("--cleanup", type=Path, required=True)
    promote.add_argument("--decision", choices=list(ReleaseDecision), required=True)
    promote.add_argument("--reviewer", required=True)
    promote.add_argument("--rationale", required=True)
    promote.add_argument("--output", type=Path, required=True)

    run = commands.add_parser("run", help="Run the crash-safe benchmark case ledger")
    run.add_argument("--profile", choices=list(Profile), default=Profile.MOCK)
    run.add_argument("--suite", choices=list(Suite), action="append", required=True)
    run.add_argument("--formation-mode", choices=("write_free", "persistent"), default="write_free")
    run.add_argument("--env-file", type=Path)
    run.add_argument("--env-file-only", action="store_true")
    run.add_argument("--provenance-file", type=Path)
    run.add_argument("--dataset-root", type=Path, default=default_dataset_root())
    run.add_argument("--artifact-root", type=Path, required=True)
    run.add_argument("--seed", type=int, default=742)
    run.add_argument("--run-id", type=UUID)


def _run_offline(args: argparse.Namespace) -> int:
    if args.command == "validate":
        report = validate_dataset(args.root)
        output = report.as_json() + "\n"
        if args.output:
            _new_output(args.output, output)
        print(output, end="")
        return 0 if report.valid else 1
    if args.command == "compile":
        compilation = compile_dataset(args.root, seed=args.seed)
        output = compilation_json_bytes(compilation)
        _new_output(args.output, output)
        print(
            json.dumps(
                {
                    "outcome": "PASS",
                    "dataset_sha256": compilation.dataset_sha256,
                    "case_count": len(compilation.cases),
                    "cross_session_source_pairs": sum(
                        len(case.inputs.session_a_messages) // 2
                        for case in compilation.cases
                        if case.suite is Suite.CROSS_SESSION
                        and case.eligibility.status == "eligible"
                    ),
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "resume":
        manifest = ArtifactRunManifest.model_validate_json(
            (args.run_root / "manifest.json").read_text(encoding="utf-8")
        )
        store = ArtifactStore.resume(args.run_root, expected_identity=manifest.identity)
        output = store.resume_plan().model_dump_json(indent=2) + "\n"
        if args.output:
            _new_output(args.output, output)
        print(output, end="")
        return 0
    if args.command == "audit" and args.audit_command == "export":
        candidates = _load_jsonl(args.candidates, _AUDIT_CANDIDATES)
        batch = select_audit_batch(
            candidates,
            policy=AuditPolicy(
                seed=args.seed,
                sample_rate=args.sample_rate,
            ),
        )
        output = batch.model_dump_json(indent=2) + "\n"
        _new_output(args.output, output)
        print(
            json.dumps(
                {
                    "outcome": "PASS",
                    "candidate_count": len(candidates),
                    "audit_count": len(batch.selections),
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "audit" and args.audit_command == "candidates":
        candidates = build_audit_candidates(
            run_root=args.run_root,
            dataset_root=args.dataset_root,
        )
        output = "".join(
            json.dumps(
                candidate.model_dump(mode="json", exclude_none=False),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for candidate in candidates
        )
        _new_output(args.output, output)
        print(
            json.dumps(
                {
                    "outcome": "PASS",
                    "candidate_count": len(candidates),
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "audit" and args.audit_command == "import":
        candidates = _load_jsonl(args.candidates, _AUDIT_CANDIDATES)
        batch = AuditBatch.model_validate_json(args.batch.read_text(encoding="utf-8"))
        decisions = _load_jsonl(args.decisions, _AUDIT_DECISIONS)
        reconciliation = reconcile_audits(candidates, batch, decisions)
        output = reconciliation.model_dump_json(indent=2) + "\n"
        _new_output(args.output, output)
        print(
            json.dumps(
                {
                    "outcome": "PASS",
                    "reconciled_count": len(reconciliation.cases),
                    "pending_audit_count": len(reconciliation.pending_audit_ids),
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "release" and args.release_command == "scorecard":
        reconciliation = AuditReconciliation.model_validate_json(
            args.audit_reconciliation.read_text(encoding="utf-8")
        )
        scorecard = build_run_quality_evidence(
            run_root=args.run_root,
            dataset_root=args.dataset_root,
            reconciliation=reconciliation,
        )
        _new_output(args.output, scorecard.model_dump_json(indent=2) + "\n")
        print(json.dumps({"outcome": "PASS", "output": str(args.output)}, sort_keys=True))
        return 0
    if args.command == "release" and args.release_command == "confirm":
        controls = tuple(
            RunQualityEvidence.model_validate_json(path.read_text(encoding="utf-8"))
            for path in args.control
        )
        candidates = tuple(
            RunQualityEvidence.model_validate_json(path.read_text(encoding="utf-8"))
            for path in args.candidate
        )
        report = build_confirmation_report(
            component=ConfirmationComponent(args.component),
            controls=controls,
            candidates=candidates,
        )
        _new_output(args.output, report.model_dump_json(indent=2) + "\n")
        print(json.dumps({"outcome": report.verdict.value, "output": str(args.output)}))
        return 0 if report.verdict.value == "pass" else 1
    if args.command == "release" and args.release_command == "performance":
        confirmation = OfficialConfirmationReport.model_validate_json(
            args.confirmation.read_text(encoding="utf-8")
        )
        evidence = PerformanceEvidence.model_validate_json(
            args.evidence.read_text(encoding="utf-8")
        )
        report = build_performance_review(
            confirmation=confirmation,
            evidence=evidence,
            reviewer=args.reviewer,
            reviewed_at=datetime.now(UTC),
            verdict=PerformanceReviewVerdict(args.verdict),
            rationale=args.rationale,
        )
        _new_output(args.output, report.model_dump_json(indent=2) + "\n")
        print(json.dumps({"outcome": report.verdict.value, "output": str(args.output)}))
        return 0 if report.verdict is not PerformanceReviewVerdict.REJECT_REGRESSION else 1
    if args.command == "release" and args.release_command == "promote":
        confirmation = OfficialConfirmationReport.model_validate_json(
            args.confirmation.read_text(encoding="utf-8")
        )
        performance = PerformanceReview.model_validate_json(
            args.performance.read_text(encoding="utf-8")
        )
        images = ExactImageSet.model_validate_json(args.images.read_text(encoding="utf-8"))
        cleanup = CleanupEvidence.model_validate_json(args.cleanup.read_text(encoding="utf-8"))
        report = build_promotion_report(
            confirmation=confirmation,
            performance=performance,
            images=images,
            cleanup=cleanup,
            decision=ReleaseDecision(args.decision),
            reviewer=args.reviewer,
            reviewed_at=datetime.now(UTC),
            rationale=args.rationale,
        )
        _new_output(args.output, report.model_dump_json(indent=2) + "\n")
        print(json.dumps({"outcome": report.decision.value, "output": str(args.output)}))
        return 0
    if args.command == "run":
        if args.env_file_only and args.env_file is None:
            raise ValueError("file-only mode needs an explicit file")
        profile = Profile(args.profile)
        suites = tuple(Suite(value) for value in args.suite)
        if profile is not Profile.MOCK:
            suites = _require_native_run_contract(profile, suites, args.formation_mode)
        config = load_config(
            profile=profile,
            suites=suites,
            env_file=args.env_file,
            environment={} if args.env_file_only else None,
            formation_mode=args.formation_mode,
        )
        identity_config = config
        provenance = (
            RunProvenance.model_validate_json(args.provenance_file.read_text(encoding="utf-8"))
            if args.provenance_file
            else capture_local_provenance()
        )
        run_id = args.run_id
        manifest_path = args.artifact_root / "manifest.json"
        resuming = manifest_path.exists()
        if resuming:
            existing = ArtifactRunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if run_id is not None and run_id != existing.identity.run_id:
                raise ValueError("resume run ID differs from the existing artifact")
            run_id = existing.identity.run_id
        run_id = run_id or uuid4()
        plan = None
        if profile is not Profile.MOCK:
            plan_path = args.artifact_root / "diagnostics" / "isolation-plan.json"
            if resuming:
                plan = IsolationPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
                if plan.run_id != run_id:
                    raise ValueError("isolation plan belongs to another run")
            else:
                if config.database_url is None or config.memory_database_url is None:
                    raise ValueError("native runner requires both PostgreSQL database URLs")
                plan = create_isolation_plan(
                    run_id=run_id,
                    owner_token=uuid4(),
                    conversation_database_url=config.database_url,
                    memory_database_url=config.memory_database_url,
                )
            config = apply_isolation(config, plan)
        preparation, _ = prepare_benchmark_run(
            run_id=run_id,
            config=identity_config,
            provenance=provenance,
            dataset_root=args.dataset_root,
            seed=args.seed,
            isolation_sha256=isolation_plan_sha256(plan) if plan is not None else None,
        )
        store = create_or_resume_store(args.artifact_root, preparation)
        if plan is not None:
            if resuming:
                if store.load_isolation_plan() != plan:
                    raise ValueError("persisted isolation plan changed")
            else:
                store.write_isolation_plan(plan)
                store.write_isolation_ledger(new_isolation_ledger(plan))
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            if profile is Profile.MOCK:
                runner.run(execute_benchmark_cases(preparation, store, MockBenchmarkExecutor()))
                completed = benchmark_execution_complete(preparation, store)
            else:
                assert plan is not None
                base_settings = _load_application_settings(
                    args.env_file,
                    file_only=args.env_file_only,
                )
                completed = runner.run(
                    _execute_native_run(
                        config=config,
                        base_settings=base_settings,
                        plan=plan,
                        preparation=preparation,
                        store=store,
                    )
                )
        summary = store.latest_outcome_counts
        print(
            json.dumps(
                {
                    "outcome": "PASS" if completed else "NOT_RUN",
                    "profile": profile.value,
                    "quality_claim": profile is not Profile.MOCK,
                    "official": store.manifest.official,
                    "run_id": str(preparation.identity.run_id),
                    "terminal_cases": sum(summary.values()),
                    "outcomes": {key.value: value for key, value in summary.items()},
                    "artifact_root": str(args.artifact_root),
                },
                sort_keys=True,
            )
        )
        return 0 if completed else 2
    raise ValueError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _add_offline_commands(commands)
    preflight = commands.add_parser("preflight", help="Probe only dependencies needed by a suite")
    preflight.add_argument("--profile", choices=list(Profile), default=Profile.MOCK)
    preflight.add_argument("--suite", choices=list(Suite), action="append", required=True)
    preflight.add_argument(
        "--formation-mode", choices=("write_free", "persistent"), default="write_free"
    )
    preflight.add_argument("--env-file", type=Path)
    preflight.add_argument(
        "--provenance-file",
        type=Path,
        help="Strict contract-v4 runtime/harness provenance JSON; defaults to this checkout",
    )
    preflight.add_argument(
        "--env-file-only",
        action="store_true",
        help="Ignore inherited environment; requires --env-file",
    )
    preflight.add_argument("--output", type=Path, help="Create a new, sanitized JSON artifact")
    args = parser.parse_args(argv)
    # HTTP debug loggers must not dump Authorization headers or raw provider responses.
    for name in ("httpx", "httpcore", "psycopg", "dotenv.main"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if args.command != "preflight":
        try:
            return _run_offline(args)
        except (Exception, KeyboardInterrupt):
            # Native failures can carry provider payloads, credentials or connection strings.
            print(json.dumps({"outcome": "NOT_RUN", "reason": "offline_command_error"}))
            return 2
    try:
        # Check an existing output before issuing any paid requests.
        if args.output and args.output.exists():
            raise ValueError("output exists")
        if args.env_file_only and args.env_file is None:
            raise ValueError("file-only mode needs an explicit file")
        provenance = (
            RunProvenance.model_validate_json(args.provenance_file.read_text(encoding="utf-8"))
            if args.provenance_file
            else None
        )
        config = load_config(
            profile=Profile(args.profile),
            suites=tuple(Suite(s) for s in args.suite),
            env_file=args.env_file,
            environment={} if args.env_file_only else None,
            formation_mode=args.formation_mode,
        )
    except (OSError, ValueError):
        print(json.dumps({"outcome": "NOT_RUN", "reason": "configuration_error"}))
        return 2
    try:
        # Psycopg async requires a selector loop on Windows; scope it to this CLI run.
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            report = runner.run(run_preflight(config, provenance=provenance))
        source = (
            "file_only"
            if args.env_file_only
            else ("file_then_environment" if args.env_file else "environment")
        )
        report = report.model_copy(update={"config_source": source})
        output = report.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as artifact:
                artifact.write(output + "\n")
        print(output)
    except (Exception, KeyboardInterrupt):
        # Diagnostic exception text may contain credentials, DSNs or provider payloads.
        print(json.dumps({"outcome": "DEPENDENCY_ERROR", "reason": "preflight_execution_error"}))
        return 2
    return 0 if all(s.outcome == Outcome.PASS for s in report.suites) else 1


if __name__ == "__main__":
    raise SystemExit(main())
