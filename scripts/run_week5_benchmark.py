"""Offline-safe Week 5 benchmark CLI plus dependency preflight."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from pydantic import TypeAdapter

from evaluation.artifacts import ArtifactRunManifest, ArtifactStore
from evaluation.audit import (
    AuditBatch,
    AuditCandidate,
    AuditPolicy,
    HumanAuditDecision,
    reconcile_audits,
    select_audit_batch,
)
from evaluation.compiler import compilation_json_bytes, compile_dataset
from evaluation.config import load_config
from evaluation.dataset import default_dataset_root
from evaluation.models import Outcome, Profile, RunProvenance, Suite
from evaluation.preflight import run_preflight
from scripts.validate_dataset import validate_dataset

_AUDIT_CANDIDATES = TypeAdapter(list[AuditCandidate])
_AUDIT_DECISIONS = TypeAdapter(list[HumanAuditDecision])


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
    export = audit_commands.add_parser("export", help="Select the deterministic audit batch")
    export.add_argument("--candidates", type=Path, required=True)
    export.add_argument("--seed", type=int, default=742)
    export.add_argument("--sample-rate", type=float, default=0.10)
    export.add_argument("--minimum-sample", type=int, default=5)
    export.add_argument("--output", type=Path, required=True)
    import_command = audit_commands.add_parser(
        "import", help="Validate human decisions and reconcile semantic verdicts"
    )
    import_command.add_argument("--candidates", type=Path, required=True)
    import_command.add_argument("--batch", type=Path, required=True)
    import_command.add_argument("--decisions", type=Path, required=True)
    import_command.add_argument("--output", type=Path, required=True)


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
                minimum_sample_when_available=args.minimum_sample,
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
                    "pending_audit_count": len(reconciliation.pending_audit_case_ids),
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
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
        except (OSError, ValueError):
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
