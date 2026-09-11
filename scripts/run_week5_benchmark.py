"""Week 5 CLI: T5.2 implements only preflight; evaluation commands follow in T5.5."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from evaluation.config import load_config
from evaluation.models import Outcome, Profile, Suite
from evaluation.preflight import run_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="Probe only dependencies needed by a suite")
    preflight.add_argument("--profile", choices=list(Profile), default=Profile.MOCK)
    preflight.add_argument("--suite", choices=list(Suite), action="append", required=True)
    preflight.add_argument(
        "--formation-mode", choices=("write_free", "persistent"), default="write_free"
    )
    preflight.add_argument("--env-file", type=Path)
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
    try:
        # Check an existing output before issuing any paid requests.
        if args.output and args.output.exists():
            raise ValueError("output exists")
        if args.env_file_only and args.env_file is None:
            raise ValueError("file-only mode needs an explicit file")
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
            report = runner.run(run_preflight(config))
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
