import subprocess
from uuid import uuid4

import httpx
import pytest

from scripts.local.smoke_product_e2e import (
    ProductApi,
    ProductE2EError,
    _assert_safe_kira_outage,
    _compose,
    parse_args,
)


async def test_product_api_parses_completed_product_stream() -> None:
    event_id = uuid4()
    client_message_id = uuid4()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-csrf-token"] == "csrf-token"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                "event: message.started\n"
                f'data: {{"turn_id":"turn","client_message_id":"{client_message_id}"}}\n\n'
                "event: message.delta\n"
                f'data: {{"turn_id":"turn","client_message_id":"{client_message_id}",'
                '"text":"answer"}\n\n'
                "event: message.completed\n"
                f'data: {{"turn_id":"turn","client_message_id":"{client_message_id}",'
                f'"event_id":"{event_id}","replayed":false}}\n\n'
            ),
        )

    api = ProductApi(parse_args([]).product)
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api.csrf_token = "csrf-token"
    try:
        response, result = await api.send(
            "session",
            "message",
            client_message_id=client_message_id,
        )
    finally:
        await api.close()

    assert response.status_code == 200
    assert result is not None
    assert result.answer == "answer"
    assert result.event_id == event_id
    assert result.replayed is False
    assert result.failure_code is None


async def test_product_api_preserves_sanitized_stream_failure_code() -> None:
    client_message_id = uuid4()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                "event: message.started\n"
                f'data: {{"turn_id":"turn","client_message_id":"{client_message_id}"}}\n\n'
                "event: message.failed\n"
                f'data: {{"turn_id":"turn","client_message_id":"{client_message_id}",'
                '"code":"KIRA_TIMEOUT","message":"safe","retryable":true,'
                '"correlation_id":"correlation"}\n\n'
            ),
        )

    api = ProductApi(parse_args([]).product)
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api.csrf_token = "csrf-token"
    try:
        _, result = await api.send(
            "session",
            "message",
            client_message_id=client_message_id,
        )
    finally:
        await api.close()

    assert result is not None
    assert result.event_names[-1] == "message.failed"
    assert result.failure_code == "KIRA_TIMEOUT"


async def test_compose_helper_uses_optional_observability_overlay(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["input"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout="safe-output", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    options = parse_args([])

    output = await _compose(
        options,
        "exec",
        "-T",
        "gateway",
        "command",
        include_observability=True,
        stdin="secret-through-stdin",
    )

    assert output == "safe-output"
    assert observed["input"] == "secret-through-stdin"
    command = observed["command"]
    assert isinstance(command, list)
    assert command.count("-f") == 2
    assert command[-4:] == ["exec", "-T", "gateway", "command"]


async def test_compose_helper_does_not_echo_failed_process_output(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="private output",
            stderr="private error",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProductE2EError) as error:
        await _compose(parse_args([]), "ps")

    assert str(error.value) == ""


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(502, "KIRA_CONNECTION_ERROR"), (504, "KIRA_TIMEOUT")],
)
def test_kira_outage_accepts_both_sanitized_network_failures(
    status_code: int,
    code: str,
) -> None:
    response = httpx.Response(
        status_code,
        json={"code": code, "message": "safe", "retryable": True},
    )

    _assert_safe_kira_outage(response, None, "private-run-id")


def test_kira_outage_rejects_content_leak() -> None:
    response = httpx.Response(
        502,
        json={
            "code": "KIRA_CONNECTION_ERROR",
            "message": "private-run-id",
            "retryable": True,
        },
    )

    with pytest.raises(ProductE2EError):
        _assert_safe_kira_outage(response, None, "private-run-id")


def test_parse_args_rejects_non_positive_timeout() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--timeout", "0"])
