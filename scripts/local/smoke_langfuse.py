"""Verify local Langfuse ingestion for the synthetic asynchronous chat flow."""

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from scripts.local.smoke_product_stack import ProductSmokeOptions
from scripts.local.smoke_product_stack import run as run_product_smoke

DEFAULT_LANGFUSE_URL = "http://127.0.0.1:13001"
DEFAULT_LANGFUSE_PUBLIC_KEY = "lf_pk_kira_local_acceptance"
DEFAULT_LANGFUSE_SECRET_KEY = "lf_sk_kira_local_acceptance"
SEARCHABLE_METADATA_KEYS = (
    "correlation_id",
    "turn_id",
    "event_id",
    "origin_trace_id",
)
RETRIEVAL_TRACE_STAGES = {
    "chat.request",
    "conversation.read_recent",
    "memory.search",
    "context.build",
    "rewrite.generate",
    "kira.authenticate",
    "kira.chat",
    "conversation.append_turn",
    "memory_job.enqueue",
}
FORMATION_TRACE_STAGES = {
    "memory_job.process",
    "conversation.read_boundary",
    "mem0.formation",
    "mem0.receipt",
    "mem0.existing_memory.search",
    "mem0.extract",
    "mem0.extract.parse",
    "mem0.memory.embed",
    "mem0.deduplicate",
    "mem0.persist",
    "memory_job.transition",
}


class LangfuseAcceptanceError(Exception):
    """Sanitized local acceptance failure."""


@dataclass(frozen=True, slots=True)
class LangfuseAcceptanceOptions:
    product: ProductSmokeOptions
    langfuse_url: str
    public_key: str
    secret_key: str
    timeout_seconds: float
    capture_content: bool = True


async def run(options: LangfuseAcceptanceOptions) -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=1)
    product_result = await run_product_smoke(options.product)
    expected_event_ids = frozenset(str(event_id) for event_id in product_result.event_ids)

    timeout = httpx.Timeout(options.timeout_seconds)
    auth = httpx.BasicAuth(options.public_key, options.secret_key)
    async with httpx.AsyncClient(auth=auth, timeout=timeout) as client:
        chat_traces, worker_traces = await _wait_for_traces(
            client,
            options,
            started_at=started_at,
            expected_event_ids=expected_event_ids,
        )

    retrieval_trace, formation_trace = _validate_traces(
        chat_traces, worker_traces, capture_content=options.capture_content
    )

    retrieval_metadata = _trace_metadata(retrieval_trace)
    formation_metadata = _trace_metadata(formation_trace)
    print(
        "PASS langfuse_retrieval_trace "
        f"trace_id={retrieval_trace['id']} event_id={retrieval_metadata['event_id']}"
    )
    print(
        "PASS langfuse_formation_trace "
        f"trace_id={formation_trace['id']} event_id={formation_metadata['event_id']} "
        f"origin_trace_id={formation_metadata['origin_trace_id']}"
    )
    print("PASS langfuse_searchable_metadata keys=" + ",".join(SEARCHABLE_METADATA_KEYS))


async def _wait_for_traces(
    client: httpx.AsyncClient,
    options: LangfuseAcceptanceOptions,
    *,
    started_at: datetime,
    expected_event_ids: frozenset[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deadline = time.monotonic() + options.timeout_seconds
    while time.monotonic() < deadline:
        try:
            chat_traces = await _recent_traces(
                client,
                options.langfuse_url,
                observation_name="chat.request",
                started_at=started_at,
                expected_event_ids=expected_event_ids,
            )
            worker_traces = await _recent_traces(
                client,
                options.langfuse_url,
                observation_name="memory_job.process",
                started_at=started_at,
                expected_event_ids=expected_event_ids,
            )
            if {
                _trace_metadata(trace)["event_id"] for trace in chat_traces
            } == expected_event_ids and {
                _trace_metadata(trace)["event_id"] for trace in worker_traces
            } == expected_event_ids:
                _validate_traces(
                    chat_traces, worker_traces, capture_content=options.capture_content
                )
                return chat_traces, worker_traces
        except (httpx.HTTPError, json.JSONDecodeError, LangfuseAcceptanceError):
            pass
        await asyncio.sleep(0.5)
    raise LangfuseAcceptanceError


def _validate_traces(
    chat_traces: list[dict[str, Any]],
    worker_traces: list[dict[str, Any]],
    *,
    capture_content: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    all_traces = chat_traces + worker_traces
    _assert_searchable_metadata(all_traces)
    _assert_linkage(chat_traces, worker_traces)
    retrieval_trace = _trace_with_stages(
        chat_traces, RETRIEVAL_TRACE_STAGES, require_retrieval_output=capture_content
    )
    formation_trace = _trace_with_stages(worker_traces, FORMATION_TRACE_STAGES)
    if not capture_content:
        _assert_content_absent(all_traces)
    _assert_formation_generation(formation_trace, capture_content=capture_content)
    _assert_no_local_credentials(all_traces)
    return retrieval_trace, formation_trace


async def _recent_traces(
    client: httpx.AsyncClient,
    langfuse_url: str,
    *,
    observation_name: str,
    started_at: datetime,
    expected_event_ids: frozenset[str],
) -> list[dict[str, Any]]:
    response = await client.get(
        langfuse_url + "/api/public/observations",
        params={
            "name": observation_name,
            "limit": 100,
            "fromStartTime": started_at.isoformat(),
        },
    )
    if response.status_code != 200:
        raise LangfuseAcceptanceError
    payload = response.json()
    observations = payload.get("data")
    if not isinstance(observations, list):
        raise LangfuseAcceptanceError

    trace_ids: list[str] = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        trace_id = observation.get("traceId")
        start_time = _parse_timestamp(observation.get("startTime"))
        if isinstance(trace_id, str) and start_time is not None and start_time >= started_at:
            trace_ids.append(trace_id)

    traces: list[dict[str, Any]] = []
    for trace_id in dict.fromkeys(trace_ids):
        response = await client.get(langfuse_url + f"/api/public/traces/{trace_id}")
        if response.status_code != 200:
            raise LangfuseAcceptanceError
        trace = response.json()
        metadata = trace.get("metadata") if isinstance(trace, dict) else None
        if isinstance(metadata, dict) and metadata.get("event_id") in expected_event_ids:
            traces.append(trace)
    return traces


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _trace_metadata(trace: dict[str, Any]) -> dict[str, str]:
    metadata = trace.get("metadata")
    if not isinstance(metadata, dict):
        raise LangfuseAcceptanceError
    values: dict[str, str] = {}
    for key in SEARCHABLE_METADATA_KEYS:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise LangfuseAcceptanceError
        values[key] = value
    return values


def _assert_searchable_metadata(traces: list[dict[str, Any]]) -> None:
    if not traces:
        raise LangfuseAcceptanceError
    for trace in traces:
        _trace_metadata(trace)


def _assert_linkage(
    chat_traces: list[dict[str, Any]],
    worker_traces: list[dict[str, Any]],
) -> None:
    chats_by_event = {_trace_metadata(trace)["event_id"]: trace for trace in chat_traces}
    for worker_trace in worker_traces:
        worker_metadata = _trace_metadata(worker_trace)
        chat_trace = chats_by_event.get(worker_metadata["event_id"])
        if chat_trace is None:
            raise LangfuseAcceptanceError
        chat_metadata = _trace_metadata(chat_trace)
        if (
            worker_metadata["correlation_id"] != chat_metadata["correlation_id"]
            or worker_metadata["turn_id"] != chat_metadata["turn_id"]
            or worker_metadata["origin_trace_id"] != chat_trace.get("id")
        ):
            raise LangfuseAcceptanceError


def _trace_with_stages(
    traces: list[dict[str, Any]],
    required_stages: set[str],
    *,
    require_retrieval_output: bool = False,
) -> dict[str, Any]:
    for trace in traces:
        observations = trace.get("observations")
        if not isinstance(observations, list):
            continue
        names = {
            observation.get("name") for observation in observations if isinstance(observation, dict)
        }
        if required_stages <= names:
            if require_retrieval_output:
                try:
                    _assert_retrieval_output(trace)
                except LangfuseAcceptanceError:
                    continue
            return trace
    raise LangfuseAcceptanceError


def _observation(trace: dict[str, Any], name: str) -> dict[str, Any]:
    observations = trace.get("observations")
    if not isinstance(observations, list):
        raise LangfuseAcceptanceError
    matches = [
        observation
        for observation in observations
        if isinstance(observation, dict) and observation.get("name") == name
    ]
    if len(matches) != 1:
        raise LangfuseAcceptanceError
    return matches[0]


def _assert_retrieval_output(trace: dict[str, Any]) -> None:
    output = _observation(trace, "memory.search").get("output")
    if not isinstance(output, list) or not output:
        raise LangfuseAcceptanceError


def _assert_content_absent(traces: list[dict[str, Any]]) -> None:
    for trace in traces:
        observations = trace.get("observations")
        if not isinstance(observations, list):
            raise LangfuseAcceptanceError
        for observation in observations:
            if not isinstance(observation, dict):
                raise LangfuseAcceptanceError
            if observation.get("input") is not None or observation.get("output") is not None:
                raise LangfuseAcceptanceError


def _assert_formation_generation(trace: dict[str, Any], *, capture_content: bool) -> None:
    extraction = _observation(trace, "mem0.extract")
    usage = extraction.get("usage")
    metadata = extraction.get("metadata")
    attributes = metadata.get("attributes") if isinstance(metadata, dict) else None
    if (
        extraction.get("type") != "GENERATION"
        or extraction.get("model") != "local-memory-stub"
        or not isinstance(usage, dict)
        or usage.get("input") != 1
        or usage.get("output") != 1
        or not isinstance(attributes, dict)
    ):
        raise LangfuseAcceptanceError
    if capture_content and (
        extraction.get("input") is None
        or extraction.get("output") is None
        or "kira.observation.input.truncated" not in attributes
        or "kira.observation.output.truncated" not in attributes
    ):
        raise LangfuseAcceptanceError


def _assert_no_local_credentials(traces: list[dict[str, Any]]) -> None:
    payload = json.dumps(traces, ensure_ascii=False)
    for forbidden in (
        "local-stub-credential",
        "local-langfuse-only",
        "local-clickhouse-only",
        "local-minio-only",
        "local-redis-only",
        DEFAULT_LANGFUSE_SECRET_KEY,
    ):
        if forbidden in payload:
            raise LangfuseAcceptanceError


def parse_args(argv: list[str] | None = None) -> LangfuseAcceptanceOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:18080")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18200")
    parser.add_argument("--worker-url", default="http://127.0.0.1:18201")
    parser.add_argument("--mock-kira-url", default="http://127.0.0.1:18212")
    parser.add_argument("--mock-rewriter-url", default="http://127.0.0.1:18213")
    parser.add_argument("--mock-memory-llm-url", default="http://127.0.0.1:18215")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("PRODUCT_DATABASE_URL", ProductSmokeOptions().database_url),
    )
    parser.add_argument("--username", default="local-admin")
    parser.add_argument("--password", default="local-product-only")
    parser.add_argument(
        "--langfuse-url",
        default=os.environ.get("LANGFUSE_HOST", DEFAULT_LANGFUSE_URL),
    )
    parser.add_argument(
        "--langfuse-public-key",
        default=os.environ.get("LANGFUSE_PUBLIC_KEY", DEFAULT_LANGFUSE_PUBLIC_KEY),
    )
    parser.add_argument(
        "--langfuse-secret-key",
        default=os.environ.get("LANGFUSE_SECRET_KEY", DEFAULT_LANGFUSE_SECRET_KEY),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--capture-content",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Expect masked input/output; use --no-capture-content for timing/usage-only acceptance."
        ),
    )
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
        username=args.username,
        password=args.password,
        timeout_seconds=args.timeout,
    )
    return LangfuseAcceptanceOptions(
        product=product,
        langfuse_url=args.langfuse_url.rstrip("/"),
        public_key=args.langfuse_public_key,
        secret_key=args.langfuse_secret_key,
        timeout_seconds=args.timeout,
        capture_content=args.capture_content,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except Exception as error:
        print(f"FAIL langfuse_local_acceptance error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
