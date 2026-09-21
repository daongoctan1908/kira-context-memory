"""Run the comprehensive synthetic product E2E gate against Docker Compose."""

import argparse
import asyncio
import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.infrastructure.postgres.schema import (
    auth_users,
    chat_requests,
    conversation_messages,
    conversations,
    memory_jobs,
)
from scripts.local.smoke_product_stack import (
    DEFAULT_DATABASE_URL,
    EXPECTED_KIRA_TEXT,
    ProductSmokeOptions,
    _parse_product_events,
)
from scripts.local.smoke_product_stack import run as run_product_stack_smoke
from tests.support.product_cases import FOLLOW_UP_MARKER, MEMORY_MARKER, session_a_message

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPOSE_FILE = REPOSITORY_ROOT / "compose.product.yaml"
DEFAULT_OBSERVABILITY_COMPOSE_FILE = REPOSITORY_ROOT / "compose.observability.yaml"


class ProductE2EError(Exception):
    """Sanitized product E2E failure."""


@dataclass(frozen=True, slots=True)
class ProductE2EOptions:
    product: ProductSmokeOptions
    compose_file: Path = DEFAULT_COMPOSE_FILE
    observability_compose_file: Path = DEFAULT_OBSERVABILITY_COMPOSE_FILE
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ConversationRef:
    conversation_id: UUID
    session_id: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    answer: str
    event_id: UUID | None
    replayed: bool
    event_names: tuple[str, ...]
    failure_code: str | None = None


class ProductApi:
    def __init__(self, options: ProductSmokeOptions) -> None:
        self.options = options
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(options.timeout_seconds),
            follow_redirects=False,
        )
        self.csrf_token: str | None = None
        self.user_id: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self, username: str, password: str) -> str:
        response = await self.client.post(
            self.options.frontend_url + "/api/v1/auth/login",
            headers={"Origin": self.options.frontend_url},
            json={"username": username, "password": password},
        )
        if response.status_code != 200:
            raise ProductE2EError
        payload = response.json()
        user_id = payload.get("user_id")
        csrf_token = self.client.cookies.get("kira_csrf_dev")
        if not isinstance(user_id, str) or not isinstance(csrf_token, str):
            raise ProductE2EError
        self.user_id = user_id
        self.csrf_token = csrf_token
        return user_id

    async def logout(self) -> None:
        response = await self.client.post(
            self.options.frontend_url + "/api/v1/auth/logout",
            headers=self.unsafe_headers(),
        )
        if response.status_code != 204:
            raise ProductE2EError
        self.csrf_token = None
        self.user_id = None

    def unsafe_headers(self) -> dict[str, str]:
        if self.csrf_token is None:
            raise ProductE2EError
        return {
            "Origin": self.options.frontend_url,
            "X-CSRF-Token": self.csrf_token,
        }

    async def me_status(self) -> int:
        return (await self.client.get(self.options.frontend_url + "/api/v1/auth/me")).status_code

    async def create_conversation(self, title: str) -> ConversationRef:
        response = await self.client.post(
            self.options.frontend_url + "/api/v1/conversations",
            headers=self.unsafe_headers(),
            json={"title": title},
        )
        if response.status_code != 201:
            raise ProductE2EError
        payload = response.json()
        try:
            return ConversationRef(
                UUID(str(payload["conversation_id"])),
                str(payload["session_id"]),
            )
        except (KeyError, ValueError):
            raise ProductE2EError from None

    async def history(self, session_id: str) -> httpx.Response:
        return await self.client.get(
            self.options.frontend_url + f"/api/v1/conversations/{session_id}/messages",
            params={"limit": 100},
        )

    async def list_conversations(self) -> httpx.Response:
        return await self.client.get(
            self.options.frontend_url + "/api/v1/conversations",
            params={"limit": 100},
        )

    async def delete_conversation(self, session_id: str) -> None:
        response = await self.client.delete(
            self.options.frontend_url + f"/api/v1/conversations/{session_id}",
            headers=self.unsafe_headers(),
        )
        if response.status_code != 204:
            raise ProductE2EError

    async def send(
        self,
        session_id: str,
        message: str,
        *,
        client_message_id: UUID | None = None,
    ) -> tuple[httpx.Response, ChatResult | None]:
        message_id = client_message_id or uuid4()
        response = await self.client.post(
            self.options.frontend_url + f"/api/v1/conversations/{session_id}/messages",
            headers={**self.unsafe_headers(), "Accept": "text/event-stream"},
            json={"client_message_id": str(message_id), "message": message},
        )
        if response.status_code != 200:
            return response, None
        events = _parse_product_events(response.text)
        if not events or events[0][0] != "message.started":
            raise ProductE2EError
        event_names = tuple(name for name, _ in events)
        if event_names[-1] == "message.failed":
            failure_code = events[-1][1].get("code")
            if not isinstance(failure_code, str):
                raise ProductE2EError
            return response, ChatResult("", None, False, event_names, failure_code)
        if event_names[-1] != "message.completed":
            raise ProductE2EError
        completed = events[-1][1]
        event_id_value = completed.get("event_id")
        try:
            event_id = UUID(str(event_id_value)) if event_id_value is not None else None
        except ValueError:
            raise ProductE2EError from None
        answer = "".join(
            str(data["text"])
            for name, data in events
            if name == "message.delta" and isinstance(data.get("text"), str)
        )
        replayed = completed.get("replayed")
        if not isinstance(replayed, bool):
            raise ProductE2EError
        return response, ChatResult(answer, event_id, replayed, event_names)

    async def disconnect_after_first_delta(
        self,
        session_id: str,
        message: str,
        client_message_id: UUID,
    ) -> None:
        async with self.client.stream(
            "POST",
            self.options.frontend_url + f"/api/v1/conversations/{session_id}/messages",
            headers={**self.unsafe_headers(), "Accept": "text/event-stream"},
            json={"client_message_id": str(client_message_id), "message": message},
        ) as response:
            if response.status_code != 200:
                raise ProductE2EError
            frame: list[str] = []
            async for line in response.aiter_lines():
                if line:
                    frame.append(line)
                    continue
                events = _parse_product_events("\n".join(frame) + "\n\n")
                frame.clear()
                if events and events[0][0] == "message.delta":
                    return
        raise ProductE2EError


async def run(options: ProductE2EOptions) -> None:
    run_id = uuid4().hex[:10]
    engine = create_async_engine(options.product.database_url, pool_pre_ping=True)
    admin = ProductApi(options.product)
    second = ProductApi(options.product)
    cleanup_sessions: list[str] = []
    second_username = f"e2e-{run_id}"
    second_password = "local-e2e-password"
    reset_password = "local-e2e-reset-password"
    forbidden_log_values = (
        run_id,
        second_password,
        reset_password,
        MEMORY_MARKER,
        FOLLOW_UP_MARKER,
    )
    second_user_id: UUID | None = None
    try:
        # A prior overlay/build may have recreated Gateway without recreating
        # the Nginx frontend, leaving its resolved upstream address stale.
        # Normalize the three product containers before exercising the API.
        await _restore_base_runtime(options)
        await _require_stack(admin.client, options)
        await admin.login(options.product.username, options.product.password)

        second_user_id = await _auth_and_isolation_gate(
            options,
            admin,
            second,
            second_username,
            second_password,
            reset_password,
            run_id,
        )
        print("PASS product_auth_admin_and_user_isolation")

        # Reuse the T10.1 formation/cross-conversation assertions under the
        # per-run user so an existing local-admin memory cannot contaminate
        # retrieval evidence on a reused Compose volume.
        await run_product_stack_smoke(
            replace(
                options.product,
                username=second_username,
                password=reset_password,
            )
        )

        durable = await _stream_retry_history_gate(
            options,
            engine,
            admin,
            run_id,
        )
        cleanup_sessions.append(durable.session_id)
        print("PASS product_concurrent_and_completed_retry messages=2 kira_calls=1")

        cancelled = await _cancel_retry_gate(options, engine, admin, run_id)
        cleanup_sessions.append(cancelled.session_id)
        print("PASS product_disconnect_retry cancelled_then_completed attempts=2")

        await _deletion_late_write_gate(options, engine, admin, run_id)
        print("PASS product_delete_blocks_late_memory_write")

        await _restart_gate(options, admin, durable)
        print("PASS product_restart_preserves_auth_and_history")

        await _kira_outage_gate(options, engine, admin, run_id)
        print("PASS product_kira_outage_retry recovered=true")

        await _postgres_outage_gate(options, admin, run_id)
        print("PASS product_postgres_outage_recovery sanitized=true")

        # The telemetry overlay recreates Gateway and Worker, so inspect the
        # pre-overlay container logs before Docker discards them.
        await _assert_logs_sanitized(options, forbidden=forbidden_log_values)
        await _telemetry_outage_gate(options, admin, run_id)
        print("PASS product_telemetry_outage_fail_open")

        await _assert_logs_sanitized(
            options,
            forbidden=forbidden_log_values,
        )
        print("PASS product_logs_sanitized")
    finally:
        try:
            await _release_fault_controls(admin.client, options.product)
            await _restore_base_runtime(options)
            await _best_effort_login(admin, options.product.username, options.product.password)
            for session_id in reversed(cleanup_sessions):
                try:
                    await admin.delete_conversation(session_id)
                except (httpx.HTTPError, ProductE2EError):
                    pass
            if second_user_id is not None:
                await _delete_test_user(engine, second_user_id)
        finally:
            await admin.close()
            await second.close()
            await engine.dispose()


async def _auth_and_isolation_gate(
    options: ProductE2EOptions,
    admin: ProductApi,
    second: ProductApi,
    username: str,
    password: str,
    reset_password: str,
    run_id: str,
) -> UUID:
    await _auth_admin(options, "create", username, password=password)
    user_id = UUID(await second.login(username, password))
    await second.logout()
    if await second.me_status() != 401:
        raise ProductE2EError

    await second.login(username, password)
    await _auth_admin(options, "reset-password", username, password=reset_password)
    if await second.me_status() != 401:
        raise ProductE2EError
    rejected = await second.client.post(
        options.product.frontend_url + "/api/v1/auth/login",
        headers={"Origin": options.product.frontend_url},
        json={"username": username, "password": password},
    )
    if rejected.status_code != 401:
        raise ProductE2EError
    await second.login(username, reset_password)
    await _auth_admin(options, "revoke-sessions", username)
    if await second.me_status() != 401:
        raise ProductE2EError
    await second.login(username, reset_password)

    owned = await admin.create_conversation(f"E2E isolation {run_id}")
    try:
        if (await second.history(owned.session_id)).status_code != 404:
            raise ProductE2EError
        listed = await second.list_conversations()
        if listed.status_code != 200:
            raise ProductE2EError
        sessions = {item["session_id"] for item in listed.json()["items"]}
        if owned.session_id in sessions:
            raise ProductE2EError
    finally:
        await admin.delete_conversation(owned.session_id)
    return user_id


async def _stream_retry_history_gate(
    options: ProductE2EOptions,
    engine: AsyncEngine,
    admin: ProductApi,
    run_id: str,
) -> ConversationRef:
    await _reset_kira(admin.client, options.product, block_after=1)
    conversation = await admin.create_conversation(f"E2E durable retry {run_id}")
    message_id = uuid4()
    message = f"Synthetic concurrent retry {run_id}"
    first = asyncio.create_task(
        admin.send(conversation.session_id, message, client_message_id=message_id)
    )
    await _wait_for_kira_block(admin.client, options.product, options.timeout_seconds)
    duplicate, duplicate_result = await admin.send(
        conversation.session_id,
        message,
        client_message_id=message_id,
    )
    if duplicate.status_code != 409 or duplicate_result is not None:
        raise ProductE2EError
    if duplicate.json().get("code") != "REQUEST_IN_PROGRESS":
        raise ProductE2EError
    await _release_kira(admin.client, options.product)
    first_response, first_result = await first
    if (
        first_response.status_code != 200
        or first_result is None
        or first_result.answer != EXPECTED_KIRA_TEXT
        or first_result.replayed
    ):
        raise ProductE2EError

    replay_response, replay = await admin.send(
        conversation.session_id,
        message,
        client_message_id=message_id,
    )
    if (
        replay_response.status_code != 200
        or replay is None
        or replay.answer != EXPECTED_KIRA_TEXT
        or not replay.replayed
    ):
        raise ProductE2EError
    evidence = (await admin.client.get(options.product.mock_kira_url + "/_test/requests")).json()
    if evidence.get("request_count") != 1:
        raise ProductE2EError
    if first_result.event_id is None:
        raise ProductE2EError
    await _wait_for_job_status(
        engine,
        first_result.event_id,
        "completed",
        options.timeout_seconds,
    )
    history = await admin.history(conversation.session_id)
    if history.status_code != 200 or len(history.json().get("items", [])) != 2:
        raise ProductE2EError
    status, attempts, message_count = await _request_evidence(
        engine,
        conversation.session_id,
        message_id,
    )
    if (status, attempts, message_count) != ("completed", 1, 2):
        raise ProductE2EError
    return conversation


async def _cancel_retry_gate(
    options: ProductE2EOptions,
    engine: AsyncEngine,
    admin: ProductApi,
    run_id: str,
) -> ConversationRef:
    await _reset_kira(admin.client, options.product, block_after=1)
    conversation = await admin.create_conversation(f"E2E cancel retry {run_id}")
    message_id = uuid4()
    message = f"Synthetic disconnect {run_id}"
    await admin.disconnect_after_first_delta(conversation.session_id, message, message_id)
    await _wait_for_request_status(
        engine,
        conversation.session_id,
        message_id,
        "cancelled",
        options.timeout_seconds,
    )
    status, attempts, message_count = await _request_evidence(
        engine,
        conversation.session_id,
        message_id,
    )
    if (status, attempts, message_count) != ("cancelled", 1, 0):
        raise ProductE2EError
    await _release_kira(admin.client, options.product)
    await _reset_kira(admin.client, options.product)
    response, completed = await admin.send(
        conversation.session_id,
        message,
        client_message_id=message_id,
    )
    if (
        response.status_code != 200
        or completed is None
        or completed.answer != EXPECTED_KIRA_TEXT
        or completed.replayed
    ):
        raise ProductE2EError
    status, attempts, message_count = await _request_evidence(
        engine,
        conversation.session_id,
        message_id,
    )
    if (status, attempts, message_count) != ("completed", 2, 2):
        raise ProductE2EError
    if completed.event_id is None:
        raise ProductE2EError
    await _wait_for_job_status(
        engine,
        completed.event_id,
        "completed",
        options.timeout_seconds,
    )
    return conversation


async def _deletion_late_write_gate(
    options: ProductE2EOptions,
    engine: AsyncEngine,
    admin: ProductApi,
    run_id: str,
) -> None:
    reset = await admin.client.post(
        options.product.mock_memory_llm_url + "/_test/reset",
        json={"block": True},
    )
    if reset.status_code != 200:
        raise ProductE2EError
    conversation = await admin.create_conversation(f"E2E delete fence {run_id}")
    response, result = await admin.send(
        conversation.session_id,
        session_a_message(f"{run_id}-delete"),
    )
    if response.status_code != 200 or result is None or result.event_id is None:
        raise ProductE2EError
    await _wait_for_memory_block(admin.client, options.product, options.timeout_seconds)
    await admin.delete_conversation(conversation.session_id)
    await admin.client.post(options.product.mock_memory_llm_url + "/_test/release")
    await asyncio.sleep(0.5)
    if (await admin.history(conversation.session_id)).status_code != 404:
        raise ProductE2EError
    counts = await _deleted_conversation_counts(
        engine,
        conversation.conversation_id,
        result.event_id,
    )
    if counts != (0, 0, 0, 0):
        raise ProductE2EError


async def _restart_gate(
    options: ProductE2EOptions,
    admin: ProductApi,
    conversation: ConversationRef,
) -> None:
    before = await admin.history(conversation.session_id)
    if before.status_code != 200 or len(before.json().get("items", [])) != 2:
        raise ProductE2EError
    await _compose(options, "restart", "gateway", "worker", "frontend")
    await _wait_for_runtime(admin.client, options)
    if await admin.me_status() != 200:
        raise ProductE2EError
    after = await admin.history(conversation.session_id)
    if after.status_code != 200 or after.json().get("items") != before.json().get("items"):
        raise ProductE2EError


async def _kira_outage_gate(
    options: ProductE2EOptions,
    engine: AsyncEngine,
    admin: ProductApi,
    run_id: str,
) -> None:
    conversation = await admin.create_conversation(f"E2E KiRa outage {run_id}")
    message_id = uuid4()
    try:
        await _compose(options, "stop", "mock-kira")
        response, result = await admin.send(
            conversation.session_id,
            f"Synthetic KiRa outage {run_id}",
            client_message_id=message_id,
        )
        _assert_safe_kira_outage(response, result, run_id)
        await _compose(options, "up", "-d", "--no-deps", "--wait", "mock-kira")
        response, result = await admin.send(
            conversation.session_id,
            f"Synthetic KiRa outage {run_id}",
            client_message_id=message_id,
        )
        if response.status_code != 200 or result is None or result.answer != EXPECTED_KIRA_TEXT:
            raise ProductE2EError
        status, attempts, message_count = await _request_evidence(
            engine,
            conversation.session_id,
            message_id,
        )
        if (status, attempts, message_count) != ("completed", 2, 2):
            raise ProductE2EError
    finally:
        await _compose(options, "up", "-d", "--no-deps", "--wait", "mock-kira")
        await admin.delete_conversation(conversation.session_id)


def _assert_safe_kira_outage(
    response: httpx.Response,
    result: ChatResult | None,
    run_id: str,
) -> None:
    if result is not None:
        raise ProductE2EError
    payload = response.json()
    expected_failure = {
        502: "KIRA_CONNECTION_ERROR",
        504: "KIRA_TIMEOUT",
    }
    if (
        expected_failure.get(response.status_code) != payload.get("code")
        or payload.get("retryable") is not True
        or run_id in response.text
    ):
        raise ProductE2EError


async def _postgres_outage_gate(
    options: ProductE2EOptions,
    admin: ProductApi,
    run_id: str,
) -> None:
    recovery = await admin.create_conversation(f"E2E PostgreSQL recovery {run_id}")
    try:
        await _compose(options, "stop", "postgres")
        await _wait_for_status(
            admin.client,
            options.product.worker_url + "/ready",
            503,
            options.timeout_seconds,
        )
        response = await admin.list_conversations()
        if response.status_code != 503:
            raise ProductE2EError
        payload = response.json()
        if payload.get("retryable") is not True or "local-product-only" in response.text:
            raise ProductE2EError
    finally:
        await _compose(options, "up", "-d", "--wait", "postgres")
        await _wait_for_runtime(admin.client, options)
    if await admin.me_status() != 200:
        raise ProductE2EError
    response, result = await admin.send(
        recovery.session_id,
        f"Synthetic PostgreSQL recovery {run_id}",
    )
    if response.status_code != 200 or result is None or result.answer != EXPECTED_KIRA_TEXT:
        raise ProductE2EError
    await admin.delete_conversation(recovery.session_id)


async def _telemetry_outage_gate(
    options: ProductE2EOptions,
    admin: ProductApi,
    run_id: str,
) -> None:
    await _compose(
        options,
        "up",
        "-d",
        "--no-build",
        "--wait",
        "--force-recreate",
        "gateway",
        "worker",
        "frontend",
        "otel-collector",
        include_observability=True,
    )
    await _wait_for_runtime(admin.client, options)
    await _wait_for_status(
        admin.client,
        options.product.frontend_url + "/healthz",
        200,
        options.timeout_seconds,
    )
    await _login_after_restart(
        admin,
        options.product.username,
        options.product.password,
        options.timeout_seconds,
    )
    await _compose(options, "stop", "otel-collector", include_observability=True)
    conversation = await admin.create_conversation(f"E2E telemetry outage {run_id}")
    try:
        response, result = await admin.send(
            conversation.session_id,
            f"Synthetic telemetry outage {run_id}",
        )
        if response.status_code != 200 or result is None or result.answer != EXPECTED_KIRA_TEXT:
            raise ProductE2EError
        if (await admin.client.get(options.product.gateway_url + "/ready")).status_code != 200 or (
            await admin.client.get(options.product.worker_url + "/ready")
        ).status_code != 200:
            raise ProductE2EError
    finally:
        await admin.delete_conversation(conversation.session_id)


async def _restore_base_runtime(options: ProductE2EOptions) -> None:
    try:
        await _compose(
            options,
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            "--wait",
            "gateway",
            "worker",
            "frontend",
        )
        await _compose(
            options,
            "rm",
            "-s",
            "-f",
            "otel-collector",
            include_observability=True,
        )
    except ProductE2EError:
        pass


async def _require_stack(client: httpx.AsyncClient, options: ProductE2EOptions) -> None:
    await _wait_for_runtime(client, options)
    for url in (
        options.product.frontend_url + "/healthz",
        options.product.mock_kira_url + "/health",
        options.product.mock_rewriter_url + "/health",
        options.product.mock_memory_llm_url + "/health",
    ):
        if (await client.get(url)).status_code != 200:
            raise ProductE2EError


async def _wait_for_runtime(client: httpx.AsyncClient, options: ProductE2EOptions) -> None:
    for url in (options.product.gateway_url + "/ready", options.product.worker_url + "/ready"):
        await _wait_for_status(client, url, 200, options.timeout_seconds)


async def _wait_for_status(
    client: httpx.AsyncClient,
    url: str,
    status: int,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if (await client.get(url)).status_code == status:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise ProductE2EError


async def _reset_kira(
    client: httpx.AsyncClient,
    product: ProductSmokeOptions,
    *,
    block_after: int | None = None,
    failures: int = 0,
) -> None:
    response = await client.post(
        product.mock_kira_url + "/_test/reset",
        json={"block_after_text_fragments": block_after, "failures": failures},
    )
    if response.status_code != 200:
        raise ProductE2EError


async def _release_kira(client: httpx.AsyncClient, product: ProductSmokeOptions) -> None:
    if (await client.post(product.mock_kira_url + "/_test/release")).status_code != 200:
        raise ProductE2EError


async def _wait_for_kira_block(
    client: httpx.AsyncClient,
    product: ProductSmokeOptions,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = (await client.get(product.mock_kira_url + "/_test/requests")).json()
        blocked = payload.get("blocked_request_count")
        if isinstance(blocked, int) and blocked >= 1 and payload.get("released") is False:
            return
        await asyncio.sleep(0.05)
    raise ProductE2EError


async def _wait_for_memory_block(
    client: httpx.AsyncClient,
    product: ProductSmokeOptions,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = (await client.get(product.mock_memory_llm_url + "/_test/state")).json()
        if payload.get("blocked_request_count") == 1 and payload.get("released") is False:
            return
        await asyncio.sleep(0.05)
    raise ProductE2EError


async def _release_fault_controls(
    client: httpx.AsyncClient,
    product: ProductSmokeOptions,
) -> None:
    for url in (
        product.mock_kira_url + "/_test/release",
        product.mock_memory_llm_url + "/_test/release",
    ):
        try:
            await client.post(url)
        except httpx.HTTPError:
            pass


async def _request_evidence(
    engine: AsyncEngine,
    session_id: str,
    client_message_id: UUID,
) -> tuple[str, int, int]:
    statement = (
        select(
            chat_requests.c.status,
            chat_requests.c.attempt_count,
            func.count(conversation_messages.c.message_id),
        )
        .select_from(
            chat_requests.join(
                conversations,
                chat_requests.c.conversation_id == conversations.c.conversation_id,
            ).outerjoin(
                conversation_messages,
                (conversation_messages.c.conversation_id == conversations.c.conversation_id)
                & (conversation_messages.c.turn_id == chat_requests.c.turn_id),
            )
        )
        .where(
            conversations.c.session_id == session_id,
            chat_requests.c.client_message_id == client_message_id,
        )
        .group_by(chat_requests.c.status, chat_requests.c.attempt_count)
    )
    async with engine.connect() as connection:
        row = (await connection.execute(statement)).one()
    return str(row[0]), int(row[1]), int(row[2])


async def _wait_for_request_status(
    engine: AsyncEngine,
    session_id: str,
    client_message_id: UUID,
    expected: str,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status, _, _ = await _request_evidence(engine, session_id, client_message_id)
            if status == expected:
                return
        except (httpx.HTTPError, RuntimeError):
            pass
        await asyncio.sleep(0.05)
    raise ProductE2EError


async def _wait_for_job_status(
    engine: AsyncEngine,
    event_id: UUID,
    expected: str,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        async with engine.connect() as connection:
            status = await connection.scalar(
                select(memory_jobs.c.status).where(memory_jobs.c.event_id == event_id)
            )
        if status == expected:
            return
        await asyncio.sleep(0.05)
    raise ProductE2EError


async def _deleted_conversation_counts(
    engine: AsyncEngine,
    conversation_id: UUID,
    event_id: UUID,
) -> tuple[int, int, int, int]:
    async with engine.connect() as connection:
        conversation_count = int(
            await connection.scalar(
                select(func.count())
                .select_from(conversations)
                .where(conversations.c.conversation_id == conversation_id)
            )
            or 0
        )
        job_count = int(
            await connection.scalar(
                select(func.count())
                .select_from(memory_jobs)
                .where(memory_jobs.c.event_id == event_id)
            )
            or 0
        )
        memory_count = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM memory.memories "
                    "WHERE payload->>'conversation_id' = :conversation_id"
                ),
                {"conversation_id": str(conversation_id)},
            )
            or 0
        )
        receipt_count = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM memory.memories_formation_receipts "
                    "WHERE conversation_id = :conversation_id"
                ),
                {"conversation_id": conversation_id},
            )
            or 0
        )
    return conversation_count, job_count, memory_count, receipt_count


async def _auth_admin(
    options: ProductE2EOptions,
    command: str,
    username: str,
    *,
    password: str | None = None,
) -> None:
    arguments = [
        "exec",
        "-T",
        "gateway",
        "python",
        "-m",
        "app.admin.auth_admin",
        command,
        "--username",
        username,
    ]
    stdin = None
    if password is not None:
        arguments.append("--password-stdin")
        stdin = password + "\n"
    await _compose(options, *arguments, stdin=stdin)


async def _delete_test_user(engine: AsyncEngine, user_id: UUID) -> None:
    try:
        async with engine.begin() as connection:
            await connection.execute(delete(auth_users).where(auth_users.c.user_id == user_id))
    except Exception:
        pass


async def _best_effort_login(api: ProductApi, username: str, password: str) -> None:
    try:
        await api.login(username, password)
    except (httpx.HTTPError, ProductE2EError):
        pass


async def _login_after_restart(
    api: ProductApi,
    username: str,
    password: str,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            await api.login(username, password)
            return
        except (httpx.HTTPError, ProductE2EError):
            await asyncio.sleep(0.1)
    raise ProductE2EError


async def _assert_logs_sanitized(
    options: ProductE2EOptions,
    *,
    forbidden: tuple[str, ...],
) -> None:
    logs = await _compose(options, "logs", "--no-color", "gateway", "worker")
    if any(value in logs for value in (*forbidden, "local-product-only", "local-stub-credential")):
        raise ProductE2EError


async def _compose(
    options: ProductE2EOptions,
    *arguments: str,
    include_observability: bool = False,
    stdin: str | None = None,
) -> str:
    command = ["docker", "compose", "-f", str(options.compose_file)]
    if include_observability:
        command.extend(["-f", str(options.observability_compose_file)])
    command.extend(arguments)

    def invoke() -> str:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=max(options.timeout_seconds * 4, 120),
            check=False,
        )
        if result.returncode != 0:
            raise ProductE2EError
        return result.stdout

    return await asyncio.to_thread(invoke)


def parse_args(argv: list[str] | None = None) -> ProductE2EOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:18080")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18200")
    parser.add_argument("--worker-url", default="http://127.0.0.1:18201")
    parser.add_argument("--mock-kira-url", default="http://127.0.0.1:18212")
    parser.add_argument("--mock-rewriter-url", default="http://127.0.0.1:18213")
    parser.add_argument("--mock-memory-llm-url", default="http://127.0.0.1:18215")
    parser.add_argument(
        "--database-url", default=os.getenv("PRODUCT_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument(
        "--observability-compose-file",
        type=Path,
        default=DEFAULT_OBSERVABILITY_COMPOSE_FILE,
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    product = ProductSmokeOptions(
        frontend_url=args.frontend_url.rstrip("/"),
        gateway_url=args.gateway_url.rstrip("/"),
        worker_url=args.worker_url.rstrip("/"),
        mock_kira_url=args.mock_kira_url.rstrip("/"),
        mock_rewriter_url=args.mock_rewriter_url.rstrip("/"),
        mock_memory_llm_url=args.mock_memory_llm_url.rstrip("/"),
        database_url=args.database_url,
        username=os.getenv("PRODUCT_ADMIN_USERNAME", "local-admin"),
        password=os.getenv("PRODUCT_ADMIN_PASSWORD", "local-product-only"),
        timeout_seconds=args.timeout,
    )
    return ProductE2EOptions(
        product=product,
        compose_file=args.compose_file.resolve(),
        observability_compose_file=args.observability_compose_file.resolve(),
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except Exception as error:
        print(f"FAIL product_e2e error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
