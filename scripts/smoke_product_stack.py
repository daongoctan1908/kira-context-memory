"""Verify the local product stack through auth, frontend proxy, and async memory."""

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.models.memory_job import MemoryJobStatus
from app.infrastructure.postgres.schema import memory_jobs
from tests.support.week4_cases import (
    memory_fact,
    rewritten_follow_up,
    session_a_message,
    session_b_follow_up,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://kira:local-product-only@127.0.0.1:15434/kira_product"
EXPECTED_KIRA_TEXT = "Mock KiRa answer"


class ProductSmokeError(Exception):
    """Sanitized local product-stack acceptance failure."""


@dataclass(frozen=True, slots=True)
class ProductSmokeOptions:
    frontend_url: str = "http://127.0.0.1:18080"
    gateway_url: str = "http://127.0.0.1:18200"
    worker_url: str = "http://127.0.0.1:18201"
    mock_kira_url: str = "http://127.0.0.1:18212"
    mock_rewriter_url: str = "http://127.0.0.1:18213"
    mock_memory_llm_url: str = "http://127.0.0.1:18215"
    database_url: str = DEFAULT_DATABASE_URL
    username: str = "local-admin"
    password: str = "local-product-only"
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class ProductChatResult:
    answer: str
    event_id: UUID


async def run(options: ProductSmokeOptions) -> None:
    run_id = uuid4().hex[:12]
    fact = memory_fact(run_id)
    first_query = session_a_message(run_id)
    follow_up = session_b_follow_up(run_id)
    rewritten = rewritten_follow_up(run_id)
    engine = create_async_engine(options.database_url, pool_pre_ping=True)
    session_ids: list[str] = []
    csrf_token: str | None = None

    timeout = httpx.Timeout(options.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            await _require_stack_ready(client, options)
            user_id, csrf_token = await _login(client, options)
            await _reset_evidence(client, options)

            session_a = await _create_conversation(
                client,
                options,
                csrf_token,
                title=f"Synthetic memory source {run_id}",
            )
            session_ids.append(session_a)
            started_at = time.perf_counter()
            first = await _chat(client, options, csrf_token, session_a, first_query)
            response_seconds = time.perf_counter() - started_at
            if first.answer != EXPECTED_KIRA_TEXT:
                raise ProductSmokeError

            blocked_state = await _wait_for_memory_llm_block(client, options)
            if await _job_status(engine, first.event_id) is not MemoryJobStatus.PROCESSING:
                raise ProductSmokeError
            print(
                "PASS product_response_before_memory_release "
                f"seconds={response_seconds:.3f} blocked_requests="
                f"{blocked_state['blocked_request_count']}"
            )

            await _post_ok(client, options.mock_memory_llm_url + "/_test/release")
            await _wait_for_completed_job(engine, first.event_id, options)
            if await _memory_count(engine, user_id, fact) != 1:
                raise ProductSmokeError
            print("PASS product_formation durable_memory_count=1")

            session_b = await _create_conversation(
                client,
                options,
                csrf_token,
                title=f"Synthetic memory consumer {run_id}",
            )
            session_ids.append(session_b)
            second = await _chat(client, options, csrf_token, session_b, follow_up)
            if second.answer != EXPECTED_KIRA_TEXT:
                raise ProductSmokeError
            await _wait_for_completed_job(engine, second.event_id, options)
            await _assert_rewrite_evidence(client, options, follow_up, rewritten)
            await _assert_kira_evidence(client, options, first_query, rewritten)
            print("PASS product_cross_session_ltm recent_messages=0")

            await _assert_conversation_list(client, options, expected_session_ids=session_ids)
            print("PASS product_frontend_auth_and_history conversations=2")
        finally:
            try:
                try:
                    await client.post(options.mock_memory_llm_url + "/_test/release")
                except httpx.HTTPError:
                    pass
                if csrf_token is not None:
                    for session_id in reversed(session_ids):
                        try:
                            await _delete_conversation(
                                client,
                                options,
                                csrf_token,
                                session_id,
                            )
                        except (httpx.HTTPError, ProductSmokeError):
                            pass
            finally:
                await engine.dispose()


async def _require_stack_ready(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
) -> None:
    for url in (
        options.frontend_url + "/healthz",
        options.gateway_url + "/ready",
        options.worker_url + "/ready",
        options.mock_kira_url + "/health",
        options.mock_rewriter_url + "/health",
        options.mock_memory_llm_url + "/health",
    ):
        response = await client.get(url)
        if response.status_code != 200:
            raise ProductSmokeError


async def _login(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
) -> tuple[str, str]:
    response = await client.post(
        options.frontend_url + "/api/v1/auth/login",
        headers={"Origin": options.frontend_url},
        json={"username": options.username, "password": options.password},
    )
    if response.status_code != 200:
        raise ProductSmokeError
    payload = response.json()
    user_id = payload.get("user_id")
    csrf_token = client.cookies.get("kira_csrf_dev")
    if not isinstance(user_id, str) or not isinstance(csrf_token, str):
        raise ProductSmokeError
    return user_id, csrf_token


def _unsafe_headers(options: ProductSmokeOptions, csrf_token: str) -> dict[str, str]:
    return {
        "Origin": options.frontend_url,
        "X-CSRF-Token": csrf_token,
    }


async def _create_conversation(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
    csrf_token: str,
    *,
    title: str,
) -> str:
    response = await client.post(
        options.frontend_url + "/api/v1/conversations",
        headers=_unsafe_headers(options, csrf_token),
        json={"title": title},
    )
    payload = response.json()
    session_id = payload.get("session_id")
    if response.status_code != 201 or not isinstance(session_id, str):
        raise ProductSmokeError
    return session_id


async def _delete_conversation(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
    csrf_token: str,
    session_id: str,
) -> None:
    response = await client.delete(
        options.frontend_url + f"/api/v1/conversations/{session_id}",
        headers=_unsafe_headers(options, csrf_token),
    )
    if response.status_code != 204:
        raise ProductSmokeError


async def _chat(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
    csrf_token: str,
    session_id: str,
    message: str,
) -> ProductChatResult:
    client_message_id = str(uuid4())
    async with client.stream(
        "POST",
        options.frontend_url + f"/api/v1/conversations/{session_id}/messages",
        headers={
            **_unsafe_headers(options, csrf_token),
            "Accept": "text/event-stream",
        },
        json={"client_message_id": client_message_id, "message": message},
    ) as response:
        if response.status_code != 200 or "text/event-stream" not in response.headers.get(
            "content-type", ""
        ):
            raise ProductSmokeError
        payload = "\n".join([line async for line in response.aiter_lines()])

    events = _parse_product_events(payload)
    if not events or events[0][0] != "message.started" or events[-1][0] != "message.completed":
        raise ProductSmokeError
    if any(event_name == "message.failed" for event_name, _ in events):
        raise ProductSmokeError
    if any(data.get("client_message_id") != client_message_id for _, data in events):
        raise ProductSmokeError
    answer = "".join(
        str(data["text"])
        for event_name, data in events
        if event_name == "message.delta" and isinstance(data.get("text"), str)
    )
    completed = events[-1][1]
    try:
        event_id = UUID(str(completed["event_id"]))
    except (KeyError, ValueError):
        raise ProductSmokeError from None
    if completed.get("replayed") is not False:
        raise ProductSmokeError
    return ProductChatResult(answer, event_id)


def _parse_product_events(payload: str) -> tuple[tuple[str, dict[str, object]], ...]:
    normalized = payload.replace("\r\n", "\n")
    events: list[tuple[str, dict[str, object]]] = []
    for frame in normalized.split("\n\n"):
        name: str | None = None
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        if name is None or not data_lines:
            continue
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            raise ProductSmokeError from None
        if not isinstance(data, dict):
            raise ProductSmokeError
        events.append((name, data))
    return tuple(events)


async def _reset_evidence(client: httpx.AsyncClient, options: ProductSmokeOptions) -> None:
    await _post_ok(client, options.mock_kira_url + "/_test/reset")
    await _post_ok(client, options.mock_rewriter_url + "/_test/reset")
    response = await client.post(
        options.mock_memory_llm_url + "/_test/reset",
        json={"block": True},
    )
    if response.status_code != 200:
        raise ProductSmokeError


async def _post_ok(client: httpx.AsyncClient, url: str) -> None:
    if (await client.post(url)).status_code != 200:
        raise ProductSmokeError


async def _wait_for_memory_llm_block(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
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
    raise ProductSmokeError


async def _job_status(engine: AsyncEngine, event_id: UUID) -> MemoryJobStatus | None:
    async with engine.connect() as connection:
        value = (
            await connection.execute(
                select(memory_jobs.c.status).where(memory_jobs.c.event_id == event_id)
            )
        ).scalar_one_or_none()
    return MemoryJobStatus(value) if value is not None else None


async def _wait_for_completed_job(
    engine: AsyncEngine,
    event_id: UUID,
    options: ProductSmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        if await _job_status(engine, event_id) is MemoryJobStatus.COMPLETED:
            return
        await asyncio.sleep(0.05)
    raise ProductSmokeError


async def _memory_count(engine: AsyncEngine, user_id: str, fact: str) -> int:
    statement = text(
        "SELECT count(*) FROM memory.memories "
        "WHERE payload->>'user_id' = :user_id AND payload->>'data' = :fact"
    )
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    statement,
                    {"user_id": user_id, "fact": fact},
                )
            ).scalar_one()
        )


async def _assert_rewrite_evidence(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
    current: str,
    rewritten: str,
) -> None:
    response = await client.get(options.mock_rewriter_url + "/_test/requests")
    payload = response.json()
    requests = payload.get("requests")
    expected = {
        "current_hash": sha256(current.encode()).hexdigest(),
        "long_term_memory_count": 1,
        "output_hash": sha256(rewritten.encode()).hexdigest(),
        "recent_message_count": 0,
    }
    if (
        response.status_code != 200
        or payload.get("stub") != "rewriter-local-only"
        or requests != [expected]
    ):
        raise ProductSmokeError


async def _assert_kira_evidence(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
    first_query: str,
    rewritten: str,
) -> None:
    response = await client.get(options.mock_kira_url + "/_test/requests")
    payload = response.json()
    expected_hashes = [
        sha256(first_query.encode()).hexdigest(),
        sha256(rewritten.encode()).hexdigest(),
    ]
    if (
        response.status_code != 200
        or payload.get("stub") != "kira-week2-local-only"
        or payload.get("query_hashes") != expected_hashes
    ):
        raise ProductSmokeError


async def _assert_conversation_list(
    client: httpx.AsyncClient,
    options: ProductSmokeOptions,
    *,
    expected_session_ids: list[str],
) -> None:
    response = await client.get(
        options.frontend_url + "/api/v1/conversations",
        params={"limit": 100},
    )
    payload = response.json()
    items = payload.get("items")
    if response.status_code != 200 or not isinstance(items, list):
        raise ProductSmokeError
    actual = {
        item.get("session_id")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("session_id"), str)
    }
    if not set(expected_session_ids).issubset(actual):
        raise ProductSmokeError


def parse_args(argv: list[str] | None = None) -> ProductSmokeOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:18080")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18200")
    parser.add_argument("--worker-url", default="http://127.0.0.1:18201")
    parser.add_argument("--mock-kira-url", default="http://127.0.0.1:18212")
    parser.add_argument("--mock-rewriter-url", default="http://127.0.0.1:18213")
    parser.add_argument("--mock-memory-llm-url", default="http://127.0.0.1:18215")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("PRODUCT_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("PRODUCT_ADMIN_USERNAME", "local-admin"),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    return ProductSmokeOptions(
        frontend_url=args.frontend_url.rstrip("/"),
        gateway_url=args.gateway_url.rstrip("/"),
        worker_url=args.worker_url.rstrip("/"),
        mock_kira_url=args.mock_kira_url.rstrip("/"),
        mock_rewriter_url=args.mock_rewriter_url.rstrip("/"),
        mock_memory_llm_url=args.mock_memory_llm_url.rstrip("/"),
        database_url=args.database_url,
        username=args.username,
        password=os.environ.get("PRODUCT_ADMIN_PASSWORD", "local-product-only"),
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except Exception as error:
        print(f"FAIL product_stack_e2e error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
