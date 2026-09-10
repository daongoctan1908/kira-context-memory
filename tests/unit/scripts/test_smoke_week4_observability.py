from pathlib import Path

import pytest

from scripts.smoke_week4_async import Week4SmokeError
from scripts.smoke_week4_observability import (
    ObservabilitySmokeOptions,
    _assert_logs_sanitized,
    _compose,
    _metric_value_or_zero,
    parse_args,
)


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"stdout\n",
        stderr: bytes = b"stderr\n",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


def options(tmp_path: Path) -> ObservabilitySmokeOptions:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return ObservabilitySmokeOptions(compose_file=compose_file, timeout_seconds=1)


def test_metric_value_or_zero_distinguishes_absent_valid_and_invalid_samples() -> None:
    metric = 'example_total{outcome="error"}'

    assert _metric_value_or_zero("# no matching sample\n", metric) == 0
    assert _metric_value_or_zero(f"{metric} 2.0\n", metric) == 2

    with pytest.raises(Week4SmokeError):
        _metric_value_or_zero(f"{metric} invalid\n", metric)
    with pytest.raises(Week4SmokeError):
        _metric_value_or_zero(f"{metric} 1\n{metric} 2\n", metric)


def test_log_assertion_requires_safe_operational_evidence() -> None:
    logs = "\n".join(
        (
            '{"operation": "postgres_read", "fallback_mode": "original_query"}',
            '{"operation": "postgres_write", "fallback_mode": "answer_without_history"}',
            '{"operation": "read_memory_job_stats"}',
        )
    )

    _assert_logs_sanitized(logs, forbidden=("private sentinel",))

    with pytest.raises(Week4SmokeError):
        _assert_logs_sanitized(logs + " private sentinel", forbidden=("private sentinel",))
    with pytest.raises(Week4SmokeError):
        _assert_logs_sanitized(logs.replace("read_memory_job_stats", "other"), forbidden=())


async def test_compose_captures_both_output_streams_without_exposing_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[tuple[tuple[str, ...], Path]] = []

    async def create_process(*command: str, **kwargs: object) -> FakeProcess:
        observed.append((command, kwargs["cwd"]))  # type: ignore[arg-type]
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create_process)
    configured = options(tmp_path)

    assert await _compose(configured, "logs", "worker") == "stdout\nstderr\n"
    assert observed == [
        (
            (
                "docker",
                "compose",
                "-f",
                str(configured.compose_file),
                "logs",
                "worker",
            ),
            configured.compose_file.parent,
        )
    ]

    async def failing_process(*command: str, **kwargs: object) -> FakeProcess:
        return FakeProcess(returncode=1, stderr=b"private provider output")

    monkeypatch.setattr("asyncio.create_subprocess_exec", failing_process)
    with pytest.raises(Week4SmokeError):
        await _compose(configured, "stop", "postgres")


def test_parse_args_normalizes_urls_and_resolves_compose_file(tmp_path: Path) -> None:
    configured = options(tmp_path)

    parsed = parse_args(
        [
            "--gateway-url",
            "http://gateway.test/",
            "--worker-url",
            "http://worker.test/",
            "--mock-kira-url",
            "http://kira.test/",
            "--compose-file",
            str(configured.compose_file),
            "--timeout",
            "4.5",
        ]
    )

    assert parsed.gateway_url == "http://gateway.test"
    assert parsed.worker_url == "http://worker.test"
    assert parsed.mock_kira_url == "http://kira.test"
    assert parsed.compose_file == configured.compose_file.resolve()
    assert parsed.timeout_seconds == 4.5


def test_parse_args_rejects_missing_compose_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--compose-file", str(tmp_path / "missing.yaml")])
