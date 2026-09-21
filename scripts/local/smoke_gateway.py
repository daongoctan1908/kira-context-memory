"""Manual end-to-end smoke client for the KiRa Context Gateway."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TextIO
from uuid import uuid4

import httpx


@dataclass(frozen=True, slots=True)
class SmokeOptions:
    gateway_url: str
    session_id: str
    message: str
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class SseEvent:
    name: str
    data: str


@dataclass(slots=True)
class SmokeState:
    event_count: int = 0
    text_fragments: list[str] = field(default_factory=list)
    request_ids: set[str] = field(default_factory=set)
    message_ids: set[str] = field(default_factory=set)

    @property
    def final_text(self) -> str:
        return "".join(self.text_fragments)


def iter_sse_events(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Parse blank-line-delimited SSE events, including multi-line data fields."""
    event_name = "message"
    data_lines: list[str] = []

    for line in lines:
        if line == "":
            if data_lines:
                yield SseEvent(name=event_name, data="\n".join(data_lines))
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").lstrip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())

    if data_lines:
        yield SseEvent(name=event_name, data="\n".join(data_lines))


def run_smoke(
    options: SmokeOptions,
    *,
    transport: httpx.BaseTransport | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Call Gateway ``POST /chat`` and verify a complete streamed text response."""
    timeout = httpx.Timeout(
        connect=options.connect_timeout_seconds,
        read=options.read_timeout_seconds,
        write=options.connect_timeout_seconds,
        pool=options.connect_timeout_seconds,
    )
    chat_url = options.gateway_url.rstrip("/") + "/chat"

    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            with client.stream(
                "POST",
                chat_url,
                headers={"Accept": "text/event-stream"},
                json={"session_id": options.session_id, "message": options.message},
            ) as response:
                correlation_id = response.headers.get("X-Correlation-ID", "unknown")
                if response.status_code != 200:
                    response.read()
                    _report_http_error(response, correlation_id, stderr)
                    return 3

                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" not in content_type:
                    print(
                        f"FAIL unexpected content type: {content_type or 'missing'}",
                        file=stderr,
                    )
                    return 4

                state = SmokeState()
                for event in iter_sse_events(response.iter_lines()):
                    state.event_count += 1
                    if event.name == "gateway_error":
                        _report_gateway_error(event.data, correlation_id, stderr)
                        return 3
                    if not _process_kira_frame(event.data, state, stdout, stderr):
                        return 4

    except httpx.TimeoutException:
        print("FAIL Gateway request timed out", file=stderr)
        return 4
    except httpx.RequestError as error:
        print(f"FAIL Gateway connection error: {type(error).__name__}", file=stderr)
        return 4

    if not state.final_text:
        print("FAIL stream completed without a text response", file=stderr)
        return 2

    print(file=stdout)
    print(
        "PASS "
        f"events={state.event_count} "
        f"characters={len(state.final_text)} "
        f"correlation_id={correlation_id} "
        f"request_ids={_format_ids(state.request_ids)} "
        f"message_ids={_format_ids(state.message_ids)}",
        file=stderr,
    )
    return 0


def _process_kira_frame(
    raw_data: str,
    state: SmokeState,
    stdout: TextIO,
    stderr: TextIO,
) -> bool:
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        print("FAIL Gateway returned malformed SSE JSON", file=stderr)
        return False
    if not isinstance(payload, dict):
        print("FAIL Gateway returned a non-object SSE payload", file=stderr)
        return False

    data = payload.get("data")
    if not isinstance(data, dict):
        return True

    _add_id(data.get("requestId"), state.request_ids)
    _add_id(data.get("messageId"), state.message_ids)
    responses = data.get("response")
    if not isinstance(responses, list):
        return True

    for item in responses:
        if not isinstance(item, dict):
            continue
        response_type = item.get("type")
        content = item.get("content")
        content = content if isinstance(content, dict) else {}
        if response_type == "text":
            text = content.get("text")
            if isinstance(text, str):
                state.text_fragments.append(text)
                stdout.write(text)
                stdout.flush()
        elif response_type == "status_response":
            status_response = content.get("statusResponse")
            status_response = status_response if isinstance(status_response, dict) else {}
            name = status_response.get("name", "unknown")
            status = status_response.get("status", "unknown")
            print(f"STATUS {name}: {status}", file=stderr)
        else:
            print(f"INFO unrecognized response type: {response_type!r}", file=stderr)
    return True


def _report_http_error(response: httpx.Response, correlation_id: str, stderr: TextIO) -> None:
    code = "UNKNOWN"
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("code"), str):
            code = payload["code"]
    except ValueError:
        pass
    print(
        f"FAIL Gateway HTTP {response.status_code} code={code} correlation_id={correlation_id}",
        file=stderr,
    )


def _report_gateway_error(raw_data: str, correlation_id: str, stderr: TextIO) -> None:
    code = "UNKNOWN"
    try:
        payload = json.loads(raw_data)
        if isinstance(payload, dict) and isinstance(payload.get("code"), str):
            code = payload["code"]
    except json.JSONDecodeError:
        pass
    print(f"FAIL gateway_error code={code} correlation_id={correlation_id}", file=stderr)


def _add_id(value: object, destination: set[str]) -> None:
    if isinstance(value, str):
        destination.add(value)


def _format_ids(values: set[str]) -> str:
    return ",".join(sorted(values)) if values else "none"


def parse_args(argv: list[str] | None = None) -> SmokeOptions:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    parser.add_argument("--session-id", default=f"smoke-{uuid4().hex[:12]}")
    parser.add_argument("--message", required=True)
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--read-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    return SmokeOptions(
        gateway_url=args.gateway_url,
        session_id=args.session_id,
        message=args.message,
        connect_timeout_seconds=args.connect_timeout,
        read_timeout_seconds=args.read_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    return run_smoke(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
