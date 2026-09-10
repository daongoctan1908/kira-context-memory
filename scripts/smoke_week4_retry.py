"""Verify Week 4 transient retry, dead-letter, and operator requeue end to end."""

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
DEFAULT_COMPOSE_FILE = Path(__file__).resolve().parents[1] / "compose.week4.yaml"
EXPECTED_ERROR_CLASS = "LongTermMemoryOperationError"
PERSISTENT_FAILURE_BUDGET = 100


@dataclass(frozen=True, slots=True)
class RetrySmokeOptions:
    gateway_url: str = "http://127.0.0.1:18000"
    worker_url: str = "http://127.0.0.1:18001"
    mock_memory_llm_url: str = "http://127.0.0.1:18125"
    database_url: str = DEFAULT_DATABASE_URL
    compose_file: Path = DEFAULT_COMPOSE_FILE
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class JobEvidence:
    event_id: UUID
    status: str
    attempt_count: int
    requeue_count: int
    last_error_class: str | None
    lifecycle_event_count: int | None


@dataclass(frozen=True, slots=True)
class ProcessingMetrics:
    success: float
    retry: float
    dead: float


async def run(options: RetrySmokeOptions) -> None:
    run_id = uuid4().hex[:12]
    transient_id = f"{run_id}-transient"
    permanent_id = f"{run_id}-permanent"
    transient_session = f"w4-retry-{run_id}"
    permanent_session = f"w4-dead-{run_id}"
    transient_fact = memory_fact(transient_id)
    permanent_fact = memory_fact(permanent_id)
    engine = create_async_engine(options.database_url, pool_pre_ping=True)
    completed = False

    timeout = httpx.Timeout(options.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            await _require_stack_ready(client, options)
            baseline = await _processing_metrics(client, options)

            await _configure_failures(client, options, failures=1)
            await _require_answer(
                client,
                options,
                transient_session,
                session_a_message(transient_id),
            )
            transient = await _wait_for_job(
                engine,
                transient_session,
                options,
                status=MemoryJobStatus.COMPLETED.value,
                attempt_count=2,
                requeue_count=0,
            )
            _require_completed_job(transient)
            await _require_provider_state(
                client,
                options,
                request_count=2,
                failure_count=1,
                remaining_failures=0,
            )
            await _wait_for_metrics(
                client,
                options,
                success=baseline.success + 1,
                retry=baseline.retry + 1,
                dead=baseline.dead,
            )
            if await _memory_count(engine, transient_fact) != 1:
                raise Week4SmokeError
            print("PASS transient_failure_recovered attempts=2 lifecycle_events=1")

            await _configure_failures(
                client,
                options,
                failures=PERSISTENT_FAILURE_BUDGET,
            )
            await _require_answer(
                client,
                options,
                permanent_session,
                session_a_message(permanent_id),
            )
            dead = await _wait_for_job(
                engine,
                permanent_session,
                options,
                status=MemoryJobStatus.DEAD.value,
                attempt_count=5,
                requeue_count=0,
            )
            if (
                dead.last_error_class != EXPECTED_ERROR_CLASS
                or dead.lifecycle_event_count is not None
                or await _memory_count(engine, permanent_fact) != 0
            ):
                raise Week4SmokeError
            await _require_provider_state(
                client,
                options,
                request_count=5,
                failure_count=5,
                remaining_failures=PERSISTENT_FAILURE_BUDGET - 5,
            )
            await _wait_for_metrics(
                client,
                options,
                success=baseline.success + 1,
                retry=baseline.retry + 5,
                dead=baseline.dead + 1,
            )

            dead_payload = await _run_operator_cli(
                options,
                "list-dead",
                "--limit",
                "1000",
            )
            _require_dead_listing(
                dead_payload,
                dead,
                forbidden=(permanent_session, permanent_fact, session_a_message(permanent_id)),
            )
            print("PASS permanent_failure_dead attempts=5 provider_calls=5")

            await _configure_failures(client, options, failures=0)
            requeue_payload = await _run_operator_cli(
                options,
                "requeue",
                "--event-id",
                str(dead.event_id),
            )
            if requeue_payload != {"event_id": str(dead.event_id), "requeued": True}:
                raise Week4SmokeError
            recovered = await _wait_for_job(
                engine,
                permanent_session,
                options,
                status=MemoryJobStatus.COMPLETED.value,
                attempt_count=1,
                requeue_count=1,
            )
            _require_completed_job(recovered)
            await _require_provider_state(
                client,
                options,
                request_count=1,
                failure_count=0,
                remaining_failures=0,
            )
            await _wait_for_metrics(
                client,
                options,
                success=baseline.success + 2,
                retry=baseline.retry + 5,
                dead=baseline.dead + 1,
            )
            if await _memory_count(engine, permanent_fact) != 1:
                raise Week4SmokeError
            print("PASS operator_requeue_recovered requeue_count=1 attempts_after_requeue=1")
            completed = True
        finally:
            try:
                if completed:
                    await _cleanup(
                        engine,
                        (transient_session, permanent_session),
                        (transient_fact, permanent_fact),
                    )
            finally:
                await engine.dispose()


async def _require_stack_ready(
    client: httpx.AsyncClient,
    options: RetrySmokeOptions,
) -> None:
    for url in (
        options.gateway_url + "/ready",
        options.worker_url + "/ready",
        options.mock_memory_llm_url + "/health",
    ):
        response = await client.get(url)
        if response.status_code != 200:
            raise Week4SmokeError


async def _configure_failures(
    client: httpx.AsyncClient,
    options: RetrySmokeOptions,
    *,
    failures: int,
) -> None:
    response = await client.post(
        options.mock_memory_llm_url + "/_test/reset",
        json={"block": False, "failures": failures},
    )
    if response.status_code != 200 or response.json() != {
        "status": "reset",
        "blocking": False,
        "remaining_failures": failures,
    }:
        raise Week4SmokeError


async def _require_answer(
    client: httpx.AsyncClient,
    options: RetrySmokeOptions,
    session_id: str,
    message: str,
) -> None:
    if await _chat(client, options.gateway_url, session_id, message) != EXPECTED_KIRA_TEXT:
        raise Week4SmokeError


async def _job_evidence(engine: AsyncEngine, session_id: str) -> JobEvidence | None:
    statement = (
        select(
            memory_jobs.c.event_id,
            memory_jobs.c.status,
            memory_jobs.c.attempt_count,
            memory_jobs.c.requeue_count,
            memory_jobs.c.last_error_class,
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
    return JobEvidence(
        event_id=row.event_id,
        status=row.status,
        attempt_count=row.attempt_count,
        requeue_count=row.requeue_count,
        last_error_class=row.last_error_class,
        lifecycle_event_count=row.lifecycle_event_count,
    )


async def _wait_for_job(
    engine: AsyncEngine,
    session_id: str,
    options: RetrySmokeOptions,
    *,
    status: str,
    attempt_count: int,
    requeue_count: int,
) -> JobEvidence:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        evidence = await _job_evidence(engine, session_id)
        if (
            evidence is not None
            and evidence.status == status
            and evidence.attempt_count == attempt_count
            and evidence.requeue_count == requeue_count
        ):
            return evidence
        await asyncio.sleep(0.05)
    raise Week4SmokeError


def _require_completed_job(job: JobEvidence) -> None:
    if job.last_error_class is not None or job.lifecycle_event_count != 1:
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


async def _require_provider_state(
    client: httpx.AsyncClient,
    options: RetrySmokeOptions,
    *,
    request_count: int,
    failure_count: int,
    remaining_failures: int,
) -> None:
    response = await client.get(options.mock_memory_llm_url + "/_test/state")
    if response.status_code != 200 or response.json() != {
        "stub": "memory-llm-local-only",
        "request_count": request_count,
        "blocked_request_count": 0,
        "failure_count": failure_count,
        "remaining_failures": remaining_failures,
        "released": True,
    }:
        raise Week4SmokeError


async def _processing_metrics(
    client: httpx.AsyncClient,
    options: RetrySmokeOptions,
) -> ProcessingMetrics:
    response = await client.get(options.worker_url + "/metrics")
    if response.status_code != 200:
        raise Week4SmokeError
    return ProcessingMetrics(
        success=_metric_value(
            response.text,
            'kira_memory_job_processing_total{outcome="success"}',
        ),
        retry=_metric_value(
            response.text,
            'kira_memory_job_processing_total{outcome="retry"}',
        ),
        dead=_metric_value(
            response.text,
            'kira_memory_job_processing_total{outcome="dead"}',
        ),
    )


async def _wait_for_metrics(
    client: httpx.AsyncClient,
    options: RetrySmokeOptions,
    *,
    success: float,
    retry: float,
    dead: float,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        observed = await _processing_metrics(client, options)
        if observed.success >= success and observed.retry >= retry and observed.dead >= dead:
            return
        await asyncio.sleep(0.05)
    raise Week4SmokeError


async def _run_operator_cli(
    options: RetrySmokeOptions,
    *arguments: str,
) -> dict[str, object]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-f",
        str(options.compose_file),
        "exec",
        "-T",
        "worker",
        "kira-memory-jobs",
        *arguments,
        cwd=options.compose_file.parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=options.timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise Week4SmokeError from None
    if process.returncode != 0 or stderr.strip():
        raise Week4SmokeError
    try:
        payload = json.loads(stdout)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Week4SmokeError from error
    if not isinstance(payload, dict):
        raise Week4SmokeError
    return payload


def _require_dead_listing(
    payload: dict[str, object],
    job: JobEvidence,
    *,
    forbidden: tuple[str, ...],
) -> None:
    items = payload.get("items")
    if not isinstance(items, list):
        raise Week4SmokeError
    matching = [
        item
        for item in items
        if isinstance(item, dict) and item.get("event_id") == str(job.event_id)
    ]
    if len(matching) != 1:
        raise Week4SmokeError
    item = matching[0]
    if (
        item.get("attempt_count") != 5
        or item.get("requeue_count") != 0
        or item.get("last_error_class") != EXPECTED_ERROR_CLASS
    ):
        raise Week4SmokeError
    rendered = json.dumps(payload, ensure_ascii=False)
    if any(value in rendered for value in forbidden):
        raise Week4SmokeError


async def _cleanup(
    engine: AsyncEngine,
    session_ids: tuple[str, ...],
    facts: tuple[str, ...],
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            delete(conversations).where(
                conversations.c.user_id == SMOKE_USER_ID,
                conversations.c.session_id.in_(session_ids),
            )
        )
        for fact in facts:
            for table_name in ("memories", "memories_entities"):
                await connection.execute(
                    text(
                        f"DELETE FROM memory.{table_name} "
                        "WHERE payload->>'user_id' = :user_id "
                        "AND payload->>'data' = :fact"
                    ),
                    {"user_id": SMOKE_USER_ID, "fact": fact},
                )


def parse_args(argv: list[str] | None = None) -> RetrySmokeOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18000")
    parser.add_argument("--worker-url", default="http://127.0.0.1:18001")
    parser.add_argument("--mock-memory-llm-url", default="http://127.0.0.1:18125")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("WEEK4_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    compose_file = args.compose_file.resolve()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if not compose_file.is_file():
        parser.error("compose file does not exist")
    return RetrySmokeOptions(
        gateway_url=args.gateway_url.rstrip("/"),
        worker_url=args.worker_url.rstrip("/"),
        mock_memory_llm_url=args.mock_memory_llm_url.rstrip("/"),
        database_url=args.database_url,
        compose_file=compose_file,
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except Exception as error:
        print(f"FAIL week4_retry_e2e error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
