"""Verify Gateway degradation and Worker readiness during a PostgreSQL outage."""

import argparse
import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.infrastructure.postgres.schema import conversations
from scripts.smoke_week4_async import (
    DEFAULT_DATABASE_URL,
    EXPECTED_KIRA_TEXT,
    Week4SmokeError,
    _chat,
    _metric_value,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = REPOSITORY_ROOT / "compose.week4.yaml"
SMOKE_USER_ID = "local-week4-user"


@dataclass(frozen=True, slots=True)
class ObservabilitySmokeOptions:
    gateway_url: str = "http://127.0.0.1:18000"
    worker_url: str = "http://127.0.0.1:18001"
    mock_kira_url: str = "http://127.0.0.1:18122"
    database_url: str = DEFAULT_DATABASE_URL
    compose_file: Path = DEFAULT_COMPOSE_FILE
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class GatewayMetricSnapshot:
    postgres_read_degraded: float
    postgres_write_degraded: float
    conversation_write_error: float
    memory_schedule_error: float
    memory_search_error: float
    rewrite_bypass: float


async def run(options: ObservabilitySmokeOptions) -> None:
    run_id = uuid4().hex[:12]
    session_id = f"w4-outage-{run_id}"
    message = f"T4_E2E_OUTAGE:{run_id}: private synthetic current query"
    log_since = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    postgres_stopped = False
    engine = create_async_engine(options.database_url, pool_pre_ping=True)
    timeout = httpx.Timeout(options.timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            await _require_initial_health(client, options)
            baseline = await _gateway_metrics(client, options)
            await _reset_kira(client, options)

            await _compose(options, "stop", "postgres")
            postgres_stopped = True
            await _wait_for_worker_outage(client, options)
            print("PASS worker_readiness_reflected_queue_outage database_available=0")

            await _require_status(client, options.gateway_url + "/health", 200)
            await _require_status(client, options.gateway_url + "/ready", 200)
            if await _chat(client, options.gateway_url, session_id, message) != EXPECTED_KIRA_TEXT:
                raise Week4SmokeError
            await _require_kira_query_hash(client, options, message)
            await _wait_for_gateway_degradation(client, options, baseline)

            logs = await _compose(
                options,
                "logs",
                "--no-color",
                "--since",
                log_since,
                "gateway",
                "worker",
            )
            _assert_logs_sanitized(
                logs,
                forbidden=(
                    message,
                    session_id,
                    SMOKE_USER_ID,
                    "local-stub-credential",
                    "local-week4-only",
                ),
            )
            print("PASS gateway_degraded_without_synthetic_answer postgres_read=1 postgres_write=1")
        finally:
            try:
                if postgres_stopped:
                    await _compose(options, "up", "-d", "--wait", "postgres")
                    await _wait_for_recovery(client, options)
                    if await _conversation_count(engine, session_id) != 0:
                        raise Week4SmokeError
                    print("PASS postgresql_recovery worker_ready=1 queue_database_available=1")
            finally:
                await engine.dispose()


async def _require_initial_health(
    client: httpx.AsyncClient,
    options: ObservabilitySmokeOptions,
) -> None:
    for url in (
        options.gateway_url + "/health",
        options.gateway_url + "/ready",
        options.worker_url + "/health",
        options.worker_url + "/ready",
    ):
        await _require_status(client, url, 200)
    metrics = (await client.get(options.worker_url + "/metrics")).text
    if _metric_value(metrics, "kira_memory_job_queue_database_available") != 1:
        raise Week4SmokeError


async def _require_status(
    client: httpx.AsyncClient,
    url: str,
    expected: int,
) -> None:
    response = await client.get(url)
    if response.status_code != expected:
        raise Week4SmokeError


async def _reset_kira(
    client: httpx.AsyncClient,
    options: ObservabilitySmokeOptions,
) -> None:
    if (await client.post(options.mock_kira_url + "/_test/reset")).status_code != 200:
        raise Week4SmokeError


async def _require_kira_query_hash(
    client: httpx.AsyncClient,
    options: ObservabilitySmokeOptions,
    message: str,
) -> None:
    response = await client.get(options.mock_kira_url + "/_test/requests")
    payload = response.json()
    if response.status_code != 200 or payload.get("query_hashes") != [
        sha256(message.encode()).hexdigest()
    ]:
        raise Week4SmokeError


async def _wait_for_worker_outage(
    client: httpx.AsyncClient,
    options: ObservabilitySmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        try:
            health = await client.get(options.worker_url + "/health")
            ready = await client.get(options.worker_url + "/ready")
            metrics = await client.get(options.worker_url + "/metrics")
            if (
                health.status_code == 200
                and ready.status_code == 503
                and metrics.status_code == 200
                and _metric_value(
                    metrics.text,
                    "kira_memory_job_queue_database_available",
                )
                == 0
                and _metric_value(
                    metrics.text,
                    "kira_memory_job_database_backoff_seconds",
                )
                > 0
            ):
                return
        except (httpx.HTTPError, Week4SmokeError):
            pass
        await asyncio.sleep(0.1)
    raise Week4SmokeError


async def _wait_for_recovery(
    client: httpx.AsyncClient,
    options: ObservabilitySmokeOptions,
) -> None:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        try:
            ready = await client.get(options.worker_url + "/ready")
            metrics = await client.get(options.worker_url + "/metrics")
            gateway_ready = await client.get(options.gateway_url + "/ready")
            if (
                ready.status_code == 200
                and gateway_ready.status_code == 200
                and metrics.status_code == 200
                and _metric_value(
                    metrics.text,
                    "kira_memory_job_queue_database_available",
                )
                == 1
                and _metric_value(
                    metrics.text,
                    "kira_memory_job_database_backoff_seconds",
                )
                == 0
            ):
                return
        except (httpx.HTTPError, Week4SmokeError):
            pass
        await asyncio.sleep(0.1)
    raise Week4SmokeError


async def _gateway_metrics(
    client: httpx.AsyncClient,
    options: ObservabilitySmokeOptions,
) -> GatewayMetricSnapshot:
    response = await client.get(options.gateway_url + "/metrics")
    if response.status_code != 200:
        raise Week4SmokeError
    payload = response.text
    return GatewayMetricSnapshot(
        postgres_read_degraded=_metric_value_or_zero(
            payload,
            'kira_context_degraded_total{dependency="postgresql",operation="postgres_read"}',
        ),
        postgres_write_degraded=_metric_value_or_zero(
            payload,
            'kira_context_degraded_total{dependency="postgresql",operation="postgres_write"}',
        ),
        conversation_write_error=_metric_value_or_zero(
            payload,
            'kira_conversation_write_total{outcome="error"}',
        ),
        memory_schedule_error=_metric_value_or_zero(
            payload,
            'kira_memory_job_schedule_total{outcome="error"}',
        ),
        memory_search_error=_metric_value_or_zero(
            payload,
            'kira_memory_search_total{outcome="error"}',
        ),
        rewrite_bypass=_metric_value_or_zero(
            payload,
            'kira_context_rewrite_total{outcome="bypass"}',
        ),
    )


async def _wait_for_gateway_degradation(
    client: httpx.AsyncClient,
    options: ObservabilitySmokeOptions,
    baseline: GatewayMetricSnapshot,
) -> None:
    expected = GatewayMetricSnapshot(
        postgres_read_degraded=baseline.postgres_read_degraded + 1,
        postgres_write_degraded=baseline.postgres_write_degraded + 1,
        conversation_write_error=baseline.conversation_write_error + 1,
        memory_schedule_error=baseline.memory_schedule_error + 1,
        memory_search_error=baseline.memory_search_error + 1,
        rewrite_bypass=baseline.rewrite_bypass + 1,
    )
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        observed = await _gateway_metrics(client, options)
        if all(
            getattr(observed, field_name) >= getattr(expected, field_name)
            for field_name in GatewayMetricSnapshot.__dataclass_fields__
        ):
            return
        await asyncio.sleep(0.05)
    raise Week4SmokeError


def _metric_value_or_zero(payload: str, prefix: str) -> float:
    matches = [line for line in payload.splitlines() if line.startswith(prefix + " ")]
    if not matches:
        return 0.0
    if len(matches) != 1:
        raise Week4SmokeError
    try:
        return float(matches[0].split()[-1])
    except ValueError as error:
        raise Week4SmokeError from error


def _assert_logs_sanitized(logs: str, *, forbidden: tuple[str, ...]) -> None:
    if any(value in logs for value in forbidden):
        raise Week4SmokeError
    required_operations = (
        '"operation": "postgres_read"',
        '"operation": "postgres_write"',
        '"operation": "read_memory_job_stats"',
        '"fallback_mode": "original_query"',
        '"fallback_mode": "answer_without_history"',
    )
    if any(operation not in logs for operation in required_operations):
        raise Week4SmokeError


async def _conversation_count(engine: AsyncEngine, session_id: str) -> int:
    statement = (
        select(func.count())
        .select_from(conversations)
        .where(
            conversations.c.user_id == SMOKE_USER_ID,
            conversations.c.session_id == session_id,
        )
    )
    async with engine.connect() as connection:
        return int((await connection.execute(statement)).scalar_one())


async def _compose(options: ObservabilitySmokeOptions, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-f",
        str(options.compose_file),
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
    if process.returncode != 0:
        raise Week4SmokeError
    try:
        return stdout.decode() + stderr.decode()
    except UnicodeDecodeError as error:
        raise Week4SmokeError from error


def parse_args(argv: list[str] | None = None) -> ObservabilitySmokeOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18000")
    parser.add_argument("--worker-url", default="http://127.0.0.1:18001")
    parser.add_argument("--mock-kira-url", default="http://127.0.0.1:18122")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("WEEK4_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    compose_file = args.compose_file.resolve()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if not compose_file.is_file():
        parser.error("compose file does not exist")
    return ObservabilitySmokeOptions(
        gateway_url=args.gateway_url.rstrip("/"),
        worker_url=args.worker_url.rstrip("/"),
        mock_kira_url=args.mock_kira_url.rstrip("/"),
        database_url=args.database_url,
        compose_file=compose_file,
        timeout_seconds=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except Exception as error:
        print(f"FAIL week4_observability_e2e error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
