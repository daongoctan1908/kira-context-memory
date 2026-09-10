"""Verify crash-after-memory-write recovery through a reclaimed PostgreSQL lease."""

import argparse
import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.domain.models.memory_job import MemoryJobStatus
from app.infrastructure.postgres.schema import (
    conversation_messages,
    conversations,
    memory_jobs,
)
from scripts.smoke_week4_async import (
    DEFAULT_DATABASE_URL,
    EXPECTED_KIRA_TEXT,
    Week4SmokeError,
    _chat,
    _metric_value,
)
from tests.support.week4_cases import memory_fact, session_a_message

SMOKE_USER_ID = "local-week4-user"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = REPOSITORY_ROOT / "compose.week4.yaml"
DEFAULT_CRASH_OVERRIDE = REPOSITORY_ROOT / "compose.week4.crash.yaml"


@dataclass(frozen=True, slots=True)
class CrashSmokeOptions:
    gateway_url: str = "http://127.0.0.1:18000"
    worker_url: str = "http://127.0.0.1:18001"
    mock_memory_llm_url: str = "http://127.0.0.1:18125"
    database_url: str = DEFAULT_DATABASE_URL
    compose_file: Path = DEFAULT_COMPOSE_FILE
    crash_override: Path = DEFAULT_CRASH_OVERRIDE
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class CrashJobEvidence:
    event_id: UUID
    status: str
    attempt_count: int
    lease_expires_at: datetime | None
    lifecycle_event_count: int | None


async def run(options: CrashSmokeOptions) -> None:
    run_id = uuid4().hex[:12]
    session_id = f"w4-crash-{run_id}"
    fact = memory_fact(f"{run_id}-crash")
    message = session_a_message(f"{run_id}-crash")
    engine = create_async_engine(options.database_url, pool_pre_ping=True)
    completed = False
    crash_worker_started = False

    timeout = httpx.Timeout(options.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            await _start_crash_worker(options, force_recreate=True)
            crash_worker_started = True
            await _wait_for_url(client, options.worker_url + "/ready", 200, options)
            await _reset_blocked_provider(client, options)

            if await _chat(client, options.gateway_url, session_id, message) != EXPECTED_KIRA_TEXT:
                raise Week4SmokeError
            await _wait_for_blocked_provider(client, options, request_count=1)
            claimed = await _wait_for_job(
                engine,
                session_id,
                options,
                status=MemoryJobStatus.PROCESSING.value,
                attempt_count=1,
            )
            if claimed.lease_expires_at is None:
                raise Week4SmokeError

            lock_connection = await engine.connect()
            transaction = await lock_connection.begin()
            try:
                await _lock_job(lock_connection, claimed.event_id)
                await _release_provider(client, options)
                await _wait_for_memory_count(engine, fact, expected=1, options=options)
                await _wait_for_blocked_completion(engine, options)
                await _compose(options, True, "kill", "-s", "SIGKILL", "worker")
                print("PASS worker_crashed_after_memory_write blocked_complete=1")
            finally:
                await transaction.rollback()
                await lock_connection.close()

            stranded = await _job_evidence(engine, session_id)
            if (
                stranded is None
                or stranded.status != MemoryJobStatus.PROCESSING.value
                or stranded.attempt_count != 1
                or stranded.lifecycle_event_count is not None
            ):
                raise Week4SmokeError

            await _start_crash_worker(options, force_recreate=False)
            await _wait_for_url(client, options.worker_url + "/ready", 200, options)
            recovered = await _wait_for_job(
                engine,
                session_id,
                options,
                status=MemoryJobStatus.COMPLETED.value,
                attempt_count=2,
            )
            if recovered.lease_expires_at is not None:
                raise Week4SmokeError
            await _wait_for_memory_count(engine, fact, expected=1, options=options)
            await _require_provider_calls(client, options, expected=2)
            await _wait_for_reclaim_metrics(client, options)
            print(
                "PASS expired_lease_reclaimed attempts=2 durable_memory_count=1 "
                f"lifecycle_events={recovered.lifecycle_event_count}"
            )
            completed = True
        finally:
            try:
                try:
                    await _release_provider(client, options)
                except httpx.HTTPError:
                    pass
                if completed:
                    await _cleanup(engine, session_id, fact)
            finally:
                await engine.dispose()
                if crash_worker_started:
                    await _restore_base_worker(options)


async def _start_crash_worker(
    options: CrashSmokeOptions,
    *,
    force_recreate: bool,
) -> None:
    arguments = ["up", "-d", "--no-deps"]
    if force_recreate:
        arguments.append("--force-recreate")
    arguments.append("worker")
    await _compose(options, True, *arguments)


async def _restore_base_worker(options: CrashSmokeOptions) -> None:
    await _compose(
        options,
        False,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "worker",
    )


async def _compose(
    options: CrashSmokeOptions,
    include_override: bool,
    *arguments: str,
) -> str:
    command = ["docker", "compose", "-f", str(options.compose_file)]
    if include_override:
        command.extend(("-f", str(options.crash_override)))
    command.extend(arguments)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=options.compose_file.parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=options.timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise Week4SmokeError from None
    if process.returncode != 0:
        raise Week4SmokeError
    try:
        return stdout.decode().strip()
    except UnicodeDecodeError as error:
        raise Week4SmokeError from error


async def _wait_for_url(
    client: httpx.AsyncClient,
    url: str,
    expected_status: int,
    options: CrashSmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = await client.get(url)
            if response.status_code == expected_status:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise Week4SmokeError


async def _reset_blocked_provider(
    client: httpx.AsyncClient,
    options: CrashSmokeOptions,
) -> None:
    response = await client.post(
        options.mock_memory_llm_url + "/_test/reset",
        json={"block": True, "failures": 0},
    )
    if response.status_code != 200:
        raise Week4SmokeError


async def _release_provider(
    client: httpx.AsyncClient,
    options: CrashSmokeOptions,
) -> None:
    response = await client.post(options.mock_memory_llm_url + "/_test/release")
    if response.status_code != 200:
        raise Week4SmokeError


async def _wait_for_blocked_provider(
    client: httpx.AsyncClient,
    options: CrashSmokeOptions,
    *,
    request_count: int,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        response = await client.get(options.mock_memory_llm_url + "/_test/state")
        payload = response.json()
        if (
            response.status_code == 200
            and payload.get("request_count") == request_count
            and payload.get("blocked_request_count") == request_count
            and payload.get("released") is False
        ):
            return
        await asyncio.sleep(0.05)
    raise Week4SmokeError


async def _require_provider_calls(
    client: httpx.AsyncClient,
    options: CrashSmokeOptions,
    *,
    expected: int,
) -> None:
    response = await client.get(options.mock_memory_llm_url + "/_test/state")
    payload = response.json()
    if (
        response.status_code != 200
        or payload.get("request_count") != expected
        or payload.get("failure_count") != 0
        or payload.get("remaining_failures") != 0
    ):
        raise Week4SmokeError


async def _job_evidence(
    engine: AsyncEngine,
    session_id: str,
) -> CrashJobEvidence | None:
    statement = (
        select(
            memory_jobs.c.event_id,
            memory_jobs.c.status,
            memory_jobs.c.attempt_count,
            memory_jobs.c.lease_expires_at,
            memory_jobs.c.lifecycle_event_count,
        )
        .select_from(
            memory_jobs.join(
                conversation_messages,
                memory_jobs.c.boundary_message_id == conversation_messages.c.message_id,
            ).join(
                conversations,
                conversation_messages.c.conversation_id == conversations.c.conversation_id,
            )
        )
        .where(
            conversations.c.user_id == SMOKE_USER_ID,
            conversations.c.session_id == session_id,
        )
    )
    async with engine.connect() as connection:
        rows = (await connection.execute(statement)).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise Week4SmokeError
    row = rows[0]
    return CrashJobEvidence(
        event_id=row.event_id,
        status=row.status,
        attempt_count=row.attempt_count,
        lease_expires_at=row.lease_expires_at,
        lifecycle_event_count=row.lifecycle_event_count,
    )


async def _wait_for_job(
    engine: AsyncEngine,
    session_id: str,
    options: CrashSmokeOptions,
    *,
    status: str,
    attempt_count: int,
) -> CrashJobEvidence:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        evidence = await _job_evidence(engine, session_id)
        if (
            evidence is not None
            and evidence.status == status
            and evidence.attempt_count == attempt_count
        ):
            return evidence
        await asyncio.sleep(0.05)
    raise Week4SmokeError


async def _lock_job(connection: AsyncConnection, event_id: UUID) -> None:
    row = (
        await connection.execute(
            select(memory_jobs.c.event_id)
            .where(memory_jobs.c.event_id == event_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row != event_id:
        raise Week4SmokeError


async def _blocked_completion_count(engine: AsyncEngine) -> int:
    statement = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' "
        "AND query ~* ('UP' || 'DATE[[:space:]]+memory_jobs')"
    )
    async with engine.connect() as connection:
        return int((await connection.execute(statement)).scalar_one())


async def _wait_for_blocked_completion(
    engine: AsyncEngine,
    options: CrashSmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        if await _blocked_completion_count(engine) == 1:
            return
        await asyncio.sleep(0.05)
    raise Week4SmokeError


async def _memory_count(engine: AsyncEngine, fact: str) -> int:
    statement = text(
        "SELECT count(*) FROM memory.memories "
        "WHERE payload->>'user_id' = :user_id AND payload->>'data' = :fact"
    )
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    statement,
                    {"user_id": SMOKE_USER_ID, "fact": fact},
                )
            ).scalar_one()
        )


async def _wait_for_memory_count(
    engine: AsyncEngine,
    fact: str,
    *,
    expected: int,
    options: CrashSmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        if await _memory_count(engine, fact) == expected:
            return
        await asyncio.sleep(0.05)
    raise Week4SmokeError


async def _wait_for_reclaim_metrics(
    client: httpx.AsyncClient,
    options: CrashSmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        response = await client.get(options.worker_url + "/metrics")
        if response.status_code == 200:
            reclaimed = _metric_value(
                response.text,
                'kira_memory_job_claim_total{kind="reclaimed"}',
            )
            success = _metric_value(
                response.text,
                'kira_memory_job_processing_total{outcome="success"}',
            )
            if reclaimed >= 1 and success >= 1:
                return
        await asyncio.sleep(0.05)
    raise Week4SmokeError


async def _cleanup(engine: AsyncEngine, session_id: str, fact: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            delete(conversations).where(
                conversations.c.user_id == SMOKE_USER_ID,
                conversations.c.session_id == session_id,
            )
        )
        for table_name in ("memories", "memories_entities"):
            await connection.execute(
                text(
                    f"DELETE FROM memory.{table_name} "
                    "WHERE payload->>'user_id' = :user_id "
                    "AND payload->>'data' = :fact"
                ),
                {"user_id": SMOKE_USER_ID, "fact": fact},
            )


def parse_args(argv: list[str] | None = None) -> CrashSmokeOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18000")
    parser.add_argument("--worker-url", default="http://127.0.0.1:18001")
    parser.add_argument("--mock-memory-llm-url", default="http://127.0.0.1:18125")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("WEEK4_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--crash-override", type=Path, default=DEFAULT_CRASH_OVERRIDE)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    compose_file = args.compose_file.resolve()
    crash_override = args.crash_override.resolve()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if not compose_file.is_file() or not crash_override.is_file():
        parser.error("compose files must exist")
    return CrashSmokeOptions(
        gateway_url=args.gateway_url.rstrip("/"),
        worker_url=args.worker_url.rstrip("/"),
        mock_memory_llm_url=args.mock_memory_llm_url.rstrip("/"),
        database_url=args.database_url,
        compose_file=compose_file,
        crash_override=crash_override,
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except Exception as error:
        print(f"FAIL week4_crash_e2e error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
