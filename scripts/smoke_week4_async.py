"""Verify the synthetic Week 4 asynchronous memory happy path end to end."""

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.models.memory_job import MemoryJobStatus
from app.infrastructure.postgres.schema import (
    conversation_messages,
    conversations,
    memory_jobs,
)
from scripts.smoke_gateway import iter_sse_events
from tests.support.week4_cases import (
    memory_fact,
    rewritten_follow_up,
    session_a_message,
    session_b_follow_up,
)

SMOKE_USER_ID = "local-week4-user"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://kira:local-week4-only@127.0.0.1:15433/kira_week4"
EXPECTED_KIRA_TEXT = "Mock KiRa answer"


class Week4SmokeError(Exception):
    """Sanitized local acceptance failure."""


@dataclass(frozen=True, slots=True)
class Week4SmokeOptions:
    gateway_url: str = "http://127.0.0.1:18000"
    worker_url: str = "http://127.0.0.1:18001"
    mock_kira_url: str = "http://127.0.0.1:18122"
    mock_rewriter_url: str = "http://127.0.0.1:18123"
    mock_memory_llm_url: str = "http://127.0.0.1:18125"
    database_url: str = DEFAULT_DATABASE_URL
    timeout_seconds: float = 20.0


async def run(options: Week4SmokeOptions) -> None:
    run_id = uuid4().hex[:12]
    session_a = f"w4-a-{run_id}"
    session_b = f"w4-b-{run_id}"
    fact = memory_fact(run_id)
    first_query = session_a_message(run_id)
    follow_up = session_b_follow_up(run_id)
    rewritten = rewritten_follow_up(run_id)
    engine = create_async_engine(options.database_url, pool_pre_ping=True)
    completed = False

    timeout = httpx.Timeout(options.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            await _require_stack_ready(client, options)
            await _reset_evidence(client, options)

            started_at = time.perf_counter()
            first_answer = await _chat(client, options.gateway_url, session_a, first_query)
            response_seconds = time.perf_counter() - started_at
            if first_answer != EXPECTED_KIRA_TEXT:
                raise Week4SmokeError

            blocked_state = await _wait_for_memory_llm_block(client, options)
            statuses = await _job_statuses(engine, (session_a,))
            if statuses != (MemoryJobStatus.PROCESSING.value,):
                raise Week4SmokeError
            print(
                "PASS session_a_response_before_memory_model_release "
                f"seconds={response_seconds:.3f} blocked_requests="
                f"{blocked_state['blocked_request_count']}"
            )

            await _post_ok(client, options.mock_memory_llm_url + "/_test/release")
            await _wait_for_completed_jobs(engine, (session_a,), expected_count=1, options=options)
            if await _memory_count(engine, fact) != 1:
                raise Week4SmokeError
            print("PASS worker_completed_job durable_memory_count=1")

            second_answer = await _chat(client, options.gateway_url, session_b, follow_up)
            if second_answer != EXPECTED_KIRA_TEXT:
                raise Week4SmokeError
            await _assert_rewrite_evidence(client, options, follow_up, rewritten)
            await _assert_kira_evidence(client, options, first_query, rewritten)
            await _assert_metrics(client, options)
            await _wait_for_completed_jobs(
                engine,
                (session_a, session_b),
                expected_count=2,
                options=options,
            )
            print("PASS session_b_ltm_rewrite_to_kira recent_messages=0")
            completed = True
        finally:
            try:
                try:
                    await client.post(options.mock_memory_llm_url + "/_test/release")
                except httpx.HTTPError:
                    pass
                if completed:
                    await _cleanup(engine, (session_a, session_b), fact)
            finally:
                await engine.dispose()


async def _require_stack_ready(
    client: httpx.AsyncClient,
    options: Week4SmokeOptions,
) -> None:
    for url in (
        options.gateway_url + "/health",
        options.gateway_url + "/ready",
        options.worker_url + "/health",
        options.worker_url + "/ready",
        options.mock_kira_url + "/health",
        options.mock_rewriter_url + "/health",
        options.mock_memory_llm_url + "/health",
    ):
        response = await client.get(url)
        if response.status_code != 200:
            raise Week4SmokeError


async def _reset_evidence(client: httpx.AsyncClient, options: Week4SmokeOptions) -> None:
    await _post_ok(client, options.mock_kira_url + "/_test/reset")
    await _post_ok(client, options.mock_rewriter_url + "/_test/reset")
    response = await client.post(
        options.mock_memory_llm_url + "/_test/reset",
        json={"block": True},
    )
    if response.status_code != 200:
        raise Week4SmokeError


async def _post_ok(client: httpx.AsyncClient, url: str) -> None:
    if (await client.post(url)).status_code != 200:
        raise Week4SmokeError


async def _chat(
    client: httpx.AsyncClient,
    gateway_url: str,
    session_id: str,
    message: str,
) -> str:
    async with client.stream(
        "POST",
        gateway_url + "/chat",
        headers={"Accept": "text/event-stream"},
        json={"session_id": session_id, "message": message},
    ) as response:
        if response.status_code != 200 or "text/event-stream" not in response.headers.get(
            "content-type", ""
        ):
            raise Week4SmokeError
        lines = [line async for line in response.aiter_lines()]

    fragments: list[str] = []
    for event in iter_sse_events(lines):
        if event.name == "gateway_error":
            raise Week4SmokeError
        try:
            payload = json.loads(event.data)
            items = payload["data"]["response"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("type") == "text":
                content = item.get("content")
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    fragments.append(content["text"])
    if not fragments:
        raise Week4SmokeError
    return "".join(fragments)


async def _wait_for_memory_llm_block(
    client: httpx.AsyncClient,
    options: Week4SmokeOptions,
) -> dict[str, object]:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        response = await client.get(options.mock_memory_llm_url + "/_test/state")
        payload = response.json()
        if (
            response.status_code == 200
            and payload.get("stub") == "memory-llm-local-only"
            and payload.get("blocked_request_count") == 1
            and payload.get("released") is False
        ):
            return payload
        await asyncio.sleep(0.05)
    raise Week4SmokeError


async def _job_statuses(engine: AsyncEngine, session_ids: tuple[str, ...]) -> tuple[str, ...]:
    statement = (
        select(memory_jobs.c.status)
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
            conversations.c.session_id.in_(session_ids),
        )
        .order_by(memory_jobs.c.created_at, memory_jobs.c.event_id)
    )
    async with engine.connect() as connection:
        return tuple((await connection.execute(statement)).scalars())


async def _wait_for_completed_jobs(
    engine: AsyncEngine,
    session_ids: tuple[str, ...],
    *,
    expected_count: int,
    options: Week4SmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    expected = (MemoryJobStatus.COMPLETED.value,) * expected_count
    while time.monotonic() < deadline:
        if await _job_statuses(engine, session_ids) == expected:
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


async def _assert_rewrite_evidence(
    client: httpx.AsyncClient,
    options: Week4SmokeOptions,
    current: str,
    rewritten: str,
) -> None:
    response = await client.get(options.mock_rewriter_url + "/_test/requests")
    payload = response.json()
    requests = payload.get("requests")
    if response.status_code != 200 or payload.get("stub") != "rewriter-local-only":
        raise Week4SmokeError
    if not isinstance(requests, list) or len(requests) != 1:
        raise Week4SmokeError
    if requests[0] != {
        "current_hash": sha256(current.encode()).hexdigest(),
        "long_term_memory_count": 1,
        "output_hash": sha256(rewritten.encode()).hexdigest(),
        "recent_message_count": 0,
    }:
        raise Week4SmokeError


async def _assert_kira_evidence(
    client: httpx.AsyncClient,
    options: Week4SmokeOptions,
    first_query: str,
    rewritten: str,
) -> None:
    response = await client.get(options.mock_kira_url + "/_test/requests")
    payload = response.json()
    if payload.get("stub") != "kira-week2-local-only" or payload.get("query_hashes") != [
        sha256(first_query.encode()).hexdigest(),
        sha256(rewritten.encode()).hexdigest(),
    ]:
        raise Week4SmokeError


async def _assert_metrics(client: httpx.AsyncClient, options: Week4SmokeOptions) -> None:
    gateway_metrics = (await client.get(options.gateway_url + "/metrics")).text
    worker_metrics = (await client.get(options.worker_url + "/metrics")).text
    for metric in (
        'kira_memory_job_schedule_total{outcome="scheduled"}',
        'kira_memory_search_total{outcome="success"}',
        'kira_context_rewrite_total{outcome="success"}',
    ):
        if _metric_value(gateway_metrics, metric) < 1:
            raise Week4SmokeError
    if (
        _metric_value(
            worker_metrics,
            'kira_memory_job_processing_total{outcome="success"}',
        )
        < 1
    ):
        raise Week4SmokeError


def _metric_value(payload: str, prefix: str) -> float:
    matches = [line for line in payload.splitlines() if line.startswith(prefix + " ")]
    if len(matches) != 1:
        raise Week4SmokeError
    try:
        return float(matches[0].split()[-1])
    except ValueError as error:
        raise Week4SmokeError from error


async def _cleanup(
    engine: AsyncEngine,
    session_ids: tuple[str, ...],
    fact: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            delete(conversations).where(
                conversations.c.user_id == SMOKE_USER_ID,
                conversations.c.session_id.in_(session_ids),
            )
        )
        for table_name in ("memories", "memories_entities"):
            await connection.execute(
                text(
                    f"DELETE FROM memory.{table_name} "
                    "WHERE payload->>'user_id' = :user_id AND payload->>'data' = :fact"
                ),
                {"user_id": SMOKE_USER_ID, "fact": fact},
            )


def parse_args(argv: list[str] | None = None) -> Week4SmokeOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18000")
    parser.add_argument("--worker-url", default="http://127.0.0.1:18001")
    parser.add_argument("--mock-kira-url", default="http://127.0.0.1:18122")
    parser.add_argument("--mock-rewriter-url", default="http://127.0.0.1:18123")
    parser.add_argument("--mock-memory-llm-url", default="http://127.0.0.1:18125")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("WEEK4_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    return Week4SmokeOptions(
        gateway_url=args.gateway_url.rstrip("/"),
        worker_url=args.worker_url.rstrip("/"),
        mock_kira_url=args.mock_kira_url.rstrip("/"),
        mock_rewriter_url=args.mock_rewriter_url.rstrip("/"),
        mock_memory_llm_url=args.mock_memory_llm_url.rstrip("/"),
        database_url=args.database_url,
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except Exception as error:
        print(f"FAIL week4_async_e2e error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
