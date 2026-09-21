"""Collect KiRa responses into a resume-safe checkpoint and apply them to the dataset."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.config.settings import Settings
from app.infrastructure.kira.http_kira_client import KiraHttpAdapter
from evaluation.dataset import default_dataset_root
from evaluation.materialization import (
    MaterializationCheckpoint,
    apply_materialization,
    build_materialization_checkpoint,
    completed_task,
    failed_task,
    load_materialization_checkpoint,
    replace_checkpoint_task,
    verify_checkpoint_source,
    write_materialization_checkpoint,
)

DEFAULT_CHECKPOINT = Path("artifacts/week5/kira-materialization.json")


@dataclass(frozen=True, slots=True)
class KiraTextResponse:
    text: str
    request_ids: tuple[str, ...]
    message_ids: tuple[str, ...]


async def collect_kira_text(adapter: KiraHttpAdapter, query: str) -> KiraTextResponse:
    stream = await adapter.chat_stream(query)
    fragments: list[str] = []
    request_ids: set[str] = set()
    message_ids: set[str] = set()
    async for event in stream:
        if event.text_fragment:
            fragments.append(event.text_fragment)
        if event.request_id:
            request_ids.add(event.request_id)
        if event.message_id:
            message_ids.add(event.message_id)
    text = "".join(fragments).strip()
    if not text:
        raise ValueError("KiRa stream completed without text")
    return KiraTextResponse(
        text=text,
        request_ids=tuple(sorted(request_ids)),
        message_ids=tuple(sorted(message_ids)),
    )


async def collect_checkpoint(
    checkpoint: MaterializationCheckpoint,
    *,
    checkpoint_path: Path,
    settings: Settings,
    request_delay_seconds: float,
    max_requests: int | None,
    continue_on_error: bool,
) -> tuple[MaterializationCheckpoint, bool]:
    attempted = 0
    current = checkpoint
    async with httpx.AsyncClient(follow_redirects=False) as http_client:
        adapter = KiraHttpAdapter(http_client, settings)
        for index, task in enumerate(current.tasks):
            if task.status == "completed":
                continue
            if max_requests is not None and attempted >= max_requests:
                break
            attempted += 1
            try:
                response = await collect_kira_text(adapter, task.query)
                replacement = completed_task(
                    task,
                    response.text,
                    request_ids=response.request_ids,
                    message_ids=response.message_ids,
                )
                outcome = "PASS"
            except Exception as error:
                replacement = failed_task(task, type(error).__name__)
                outcome = "FAIL"
            current = replace_checkpoint_task(current, index, replacement)
            write_materialization_checkpoint(checkpoint_path, current)
            print(
                f"{outcome} task={task.task_id[:12]} "
                f"progress={current.completed_count}/{current.total_count} "
                f"attempt={replacement.attempt_count}"
            )
            if outcome == "FAIL" and not continue_on_error:
                return current, False
            if request_delay_seconds and current.completed_count < current.total_count:
                await asyncio.sleep(request_delay_seconds)
    return current, current.completed_count == current.total_count


def _load_settings(env_file: Path, *, file_only: bool) -> Settings:
    if file_only:
        from dotenv import dotenv_values

        raw = dotenv_values(env_file)
        values = {str(key).lower(): value for key, value in raw.items() if value is not None}
        return Settings(_env_file=None, **values)  # type: ignore[arg-type]
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="show targets without calling KiRa or writing files")
    plan.add_argument("--root", type=Path, default=default_dataset_root())

    preflight = commands.add_parser(
        "preflight",
        help="materialize exactly one KiRa query into the official resume checkpoint",
    )
    preflight.add_argument("--root", type=Path, default=default_dataset_root())
    preflight.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    preflight.add_argument("--env-file", type=Path, default=Path(".env"))
    preflight.add_argument(
        "--env-file-only",
        action="store_true",
        help="ignore inherited environment values and load configuration only from --env-file",
    )

    collect = commands.add_parser("collect", help="call KiRa and checkpoint every unique response")
    collect.add_argument("--root", type=Path, default=default_dataset_root())
    collect.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    collect.add_argument("--env-file", type=Path, default=Path(".env"))
    collect.add_argument(
        "--env-file-only",
        action="store_true",
        help="ignore inherited environment values and load configuration only from --env-file",
    )
    collect.add_argument(
        "--resume",
        action="store_true",
        help="reuse completed tasks from an existing checkpoint",
    )
    collect.add_argument("--max-requests", type=int)
    collect.add_argument("--request-delay-seconds", type=float, default=0.25)
    collect.add_argument("--continue-on-error", action="store_true")

    apply = commands.add_parser("apply", help="validate and merge a complete checkpoint")
    apply.add_argument("--root", type=Path, default=default_dataset_root())
    apply.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    apply.add_argument("--dataset-version", required=True)
    destination = apply.add_mutually_exclusive_group(required=True)
    destination.add_argument("--in-place", action="store_true")
    destination.add_argument("--output-root", type=Path)
    return parser


def _validate_cli_numbers(args: argparse.Namespace) -> None:
    if getattr(args, "max_requests", None) is not None and args.max_requests < 1:
        raise ValueError("--max-requests must be positive")
    if getattr(args, "request_delay_seconds", 0) < 0:
        raise ValueError("--request-delay-seconds must not be negative")


def _plan_counts(checkpoint: MaterializationCheckpoint) -> tuple[int, int]:
    fill_count = sum(
        reference.kind == "conversation_fill"
        for task in checkpoint.tasks
        for reference in task.references
    )
    qa_count = sum(
        reference.kind == "qa_answer" for task in checkpoint.tasks for reference in task.references
    )
    return fill_count, qa_count


def main(argv: list[str] | None = None) -> int:
    args = _base_parser().parse_args(argv)
    for logger_name in ("httpx", "httpcore", "dotenv.main"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)
    try:
        _validate_cli_numbers(args)
        if args.command == "plan":
            checkpoint = build_materialization_checkpoint(args.root)
            fills, answers = _plan_counts(checkpoint)
            print(
                f"PLAN dataset={checkpoint.dataset_id} version={checkpoint.dataset_version} "
                f"unique_queries={checkpoint.total_count} conversation_fills={fills} "
                f"qa_answers={answers}"
            )
            return 0

        if args.command == "apply":
            checkpoint = load_materialization_checkpoint(args.checkpoint)
            report = apply_materialization(
                args.root,
                checkpoint,
                dataset_version=args.dataset_version,
                output_root=None if args.in_place else args.output_root,
            )
            print(
                f"PASS materialized version={report.dataset_version} "
                f"unique_queries={report.unique_queries} "
                f"conversation_fills={report.conversation_fills} "
                f"qa_answers={report.qa_answers} output={report.output_root}"
            )
            return 0

        checkpoint_path = args.checkpoint.resolve()
        if checkpoint_path.exists():
            if args.command == "collect" and not args.resume:
                raise ValueError("checkpoint exists; pass --resume to continue it")
            checkpoint = load_materialization_checkpoint(checkpoint_path)
            verify_checkpoint_source(args.root, checkpoint)
        else:
            checkpoint = build_materialization_checkpoint(args.root)
            write_materialization_checkpoint(checkpoint_path, checkpoint)
        if args.command == "preflight" and checkpoint.completed_count:
            print(
                f"PASS checkpoint={checkpoint_path} "
                f"completed={checkpoint.completed_count}/{checkpoint.total_count} reused=true"
            )
            return 0

        settings = _load_settings(args.env_file, file_only=args.env_file_only)
        loop_factory = asyncio.SelectorEventLoop if os.name == "nt" else None
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            checkpoint, complete = runner.run(
                collect_checkpoint(
                    checkpoint,
                    checkpoint_path=checkpoint_path,
                    settings=settings,
                    request_delay_seconds=(
                        0 if args.command == "preflight" else args.request_delay_seconds
                    ),
                    max_requests=1 if args.command == "preflight" else args.max_requests,
                    continue_on_error=(
                        False if args.command == "preflight" else args.continue_on_error
                    ),
                )
            )
        if args.command == "preflight":
            passed = checkpoint.completed_count == 1
            status = "PASS" if passed else "FAIL"
            print(
                f"{status} checkpoint={checkpoint_path} "
                f"completed={checkpoint.completed_count}/{checkpoint.total_count} reused=false"
            )
            return 0 if passed else 1
        status = "COMPLETE" if complete else "INCOMPLETE"
        print(
            f"{status} checkpoint={checkpoint_path} "
            f"completed={checkpoint.completed_count}/{checkpoint.total_count}"
        )
        return 0 if complete else 1
    except (OSError, ValueError, ValidationError) as error:
        print(
            f"FAIL operation={args.command} error_class={type(error).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
