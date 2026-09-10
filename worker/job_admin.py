"""Bounded operator CLI for the durable PostgreSQL memory-job queue."""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any, TextIO, TypeVar
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.errors.conversation import ConversationStoreConfigurationError
from app.domain.errors.memory_job import (
    MemoryJobQueueConfigurationError,
    MemoryJobQueueConnectionError,
    MemoryJobQueueError,
    MemoryJobQueueOperationError,
    MemoryJobQueueProtocolError,
)
from app.domain.models.memory_job import DeadMemoryJob, MemoryJobStats
from app.domain.ports.memory_job_queue import MemoryJobQueuePort
from app.infrastructure.postgres.client import create_postgres_engine
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from worker.settings import MemoryJobAdminSettings, get_memory_job_admin_settings

DEFAULT_DEAD_JOB_LIMIT = 50
MAX_DEAD_JOB_LIMIT = 1000
_T = TypeVar("_T")


class AdminExitCode(IntEnum):
    """Stable process exit codes for automation and runbooks."""

    SUCCESS = 0
    INTERNAL_ERROR = 1
    USAGE_OR_CONFIGURATION = 2
    DEPENDENCY_ERROR = 3
    NOT_REQUEUED = 4
    INTERRUPTED = 130


class OperatorArgumentParser(argparse.ArgumentParser):
    """Keep invalid invocations machine-readable without echoing supplied values."""

    def error(self, message: str) -> None:
        _write_json({"error": "invalid_invocation"}, sys.stderr)
        raise SystemExit(AdminExitCode.USAGE_OR_CONFIGURATION)


def build_parser() -> OperatorArgumentParser:
    """Build the CLI parser without reading environment configuration."""
    parser = OperatorArgumentParser(
        prog="kira-memory-jobs",
        description="Inspect and explicitly requeue PostgreSQL memory jobs",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stats", help="print sanitized aggregate queue statistics")

    list_dead = commands.add_parser(
        "list-dead",
        help="print a bounded sanitized list of newest dead jobs",
    )
    list_dead.add_argument(
        "--limit",
        type=_bounded_dead_job_limit,
        default=DEFAULT_DEAD_JOB_LIMIT,
        help=f"number of rows to return (1-{MAX_DEAD_JOB_LIMIT}, default 50)",
    )

    requeue = commands.add_parser(
        "requeue",
        help="requeue exactly one dead job",
    )
    requeue.add_argument(
        "--event-id",
        type=_event_id,
        required=True,
        help="exact UUID of the dead job to requeue",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse one operator command."""
    return build_parser().parse_args(argv)


async def execute_command(
    args: argparse.Namespace,
    queue: MemoryJobQueuePort,
    *,
    timeout_seconds: float,
    output: TextIO | None = None,
) -> AdminExitCode:
    """Execute one already-parsed command against the application queue port."""
    target = output or sys.stdout
    if args.command == "stats":
        stats = await _bounded_call(queue.stats, timeout_seconds)
        if not isinstance(stats, MemoryJobStats):
            raise MemoryJobQueueProtocolError
        _write_json(_stats_payload(stats), target)
        return AdminExitCode.SUCCESS

    if args.command == "list-dead":
        jobs = await _bounded_call(
            lambda: queue.list_dead(limit=args.limit),
            timeout_seconds,
        )
        if not isinstance(jobs, tuple) or any(not isinstance(job, DeadMemoryJob) for job in jobs):
            raise MemoryJobQueueProtocolError
        _write_json(
            {
                "count": len(jobs),
                "items": [_dead_job_payload(job) for job in jobs],
                "limit": args.limit,
            },
            target,
        )
        return AdminExitCode.SUCCESS

    if args.command == "requeue":
        requeued = await _bounded_call(
            lambda: queue.requeue_dead(args.event_id),
            timeout_seconds,
        )
        if not isinstance(requeued, bool):
            raise MemoryJobQueueProtocolError
        _write_json(
            {"event_id": str(args.event_id), "requeued": requeued},
            target,
        )
        return AdminExitCode.SUCCESS if requeued else AdminExitCode.NOT_REQUEUED

    raise MemoryJobQueueProtocolError


async def run(
    args: argparse.Namespace,
    *,
    settings: MemoryJobAdminSettings | None = None,
    engine: AsyncEngine | None = None,
    output: TextIO | None = None,
    engine_factory: Callable[[MemoryJobAdminSettings], AsyncEngine] | None = None,
    queue_factory: Callable[[AsyncEngine], MemoryJobQueuePort] | None = None,
) -> AdminExitCode:
    """Own a minimal queue dependency lifecycle and execute one command."""
    resolved_settings = settings or get_memory_job_admin_settings()
    resolved_engine = engine
    owns_engine = resolved_engine is None
    if resolved_engine is None:
        resolved_engine = (engine_factory or create_postgres_engine)(resolved_settings)
    try:
        queue = (queue_factory or PostgresMemoryJobQueueAdapter)(resolved_engine)
        validate_schema = getattr(queue, "validate_schema", None)
        if not callable(validate_schema):
            raise MemoryJobQueueProtocolError
        await _bounded_call(validate_schema, resolved_settings.memory_job_db_timeout_seconds)
        return await execute_command(
            args,
            queue,
            timeout_seconds=resolved_settings.memory_job_db_timeout_seconds,
            output=output,
        )
    finally:
        if owns_engine:
            await resolved_engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI with sanitized errors and stable process exit codes."""
    try:
        args = parse_args(argv)
        return int(asyncio.run(run(args)))
    except KeyboardInterrupt:
        _write_error("interrupted")
        return int(AdminExitCode.INTERRUPTED)
    except (
        ValidationError,
        ConversationStoreConfigurationError,
        MemoryJobQueueConfigurationError,
    ):
        _write_error("invalid_configuration")
        return int(AdminExitCode.USAGE_OR_CONFIGURATION)
    except (MemoryJobQueueConnectionError, MemoryJobQueueOperationError):
        _write_error("queue_unavailable")
        return int(AdminExitCode.DEPENDENCY_ERROR)
    except MemoryJobQueueProtocolError:
        _write_error("invalid_queue_response")
        return int(AdminExitCode.DEPENDENCY_ERROR)
    except MemoryJobQueueError:
        _write_error("queue_operation_failed")
        return int(AdminExitCode.DEPENDENCY_ERROR)
    except Exception:
        _write_error("internal_error")
        return int(AdminExitCode.INTERNAL_ERROR)


async def _bounded_call(
    operation: Callable[[], Awaitable[_T]],
    timeout_seconds: float,
) -> _T:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await operation()
    except TimeoutError:
        raise MemoryJobQueueConnectionError from None


def _stats_payload(stats: MemoryJobStats) -> dict[str, Any]:
    return {
        "completed": stats.completed,
        "dead": stats.dead,
        "oldest_pending_age_seconds": stats.oldest_pending_age_seconds,
        "pending": stats.pending,
        "processing": stats.processing,
    }


def _dead_job_payload(job: DeadMemoryJob) -> dict[str, Any]:
    return {
        "attempt_count": job.attempt_count,
        "created_at": _utc_timestamp(job.created_at),
        "dead_at": _utc_timestamp(job.dead_at),
        "event_id": str(job.event_id),
        "last_error_class": job.last_error_class,
        "requeue_count": job.requeue_count,
    }


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_json(payload: dict[str, Any], output: TextIO) -> None:
    print(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True), file=output
    )


def _write_error(code: str) -> None:
    _write_json({"error": code}, sys.stderr)


def _bounded_dead_job_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer") from None
    if not 1 <= parsed <= MAX_DEAD_JOB_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_DEAD_JOB_LIMIT}")
    return parsed


def _event_id(value: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise argparse.ArgumentTypeError("event ID must be a UUID") from None


if __name__ == "__main__":
    raise SystemExit(main())
