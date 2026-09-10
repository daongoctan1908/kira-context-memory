from pathlib import Path

import pytest

from scripts.smoke_week4_async import Week4SmokeError
from scripts.smoke_week4_crash import CrashSmokeOptions, _compose, parse_args


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"container-id\n",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True


def options(tmp_path: Path) -> CrashSmokeOptions:
    compose_file = tmp_path / "compose.yaml"
    crash_override = tmp_path / "compose.crash.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    crash_override.write_text("services: {}\n", encoding="utf-8")
    return CrashSmokeOptions(
        compose_file=compose_file,
        crash_override=crash_override,
        timeout_seconds=1,
    )


async def test_compose_uses_override_only_for_crash_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[tuple[tuple[str, ...], Path]] = []

    async def create_process(*command: str, **kwargs: object) -> FakeProcess:
        observed.append((command, kwargs["cwd"]))  # type: ignore[arg-type]
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create_process)
    configured = options(tmp_path)

    assert await _compose(configured, True, "kill", "worker") == "container-id"
    assert await _compose(configured, False, "up", "worker") == "container-id"

    assert observed == [
        (
            (
                "docker",
                "compose",
                "-f",
                str(configured.compose_file),
                "-f",
                str(configured.crash_override),
                "kill",
                "worker",
            ),
            configured.compose_file.parent,
        ),
        (
            (
                "docker",
                "compose",
                "-f",
                str(configured.compose_file),
                "up",
                "worker",
            ),
            configured.compose_file.parent,
        ),
    ]


async def test_compose_maps_failure_to_sanitized_smoke_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def create_process(*command: str, **kwargs: object) -> FakeProcess:
        return FakeProcess(returncode=1, stdout=b"private provider output")

    monkeypatch.setattr("asyncio.create_subprocess_exec", create_process)

    with pytest.raises(Week4SmokeError):
        await _compose(options(tmp_path), True, "up", "worker")


def test_parse_args_normalizes_urls_and_resolves_both_compose_files(tmp_path: Path) -> None:
    configured = options(tmp_path)

    parsed = parse_args(
        [
            "--gateway-url",
            "http://gateway.test/",
            "--worker-url",
            "http://worker.test/",
            "--mock-memory-llm-url",
            "http://memory.test/",
            "--compose-file",
            str(configured.compose_file),
            "--crash-override",
            str(configured.crash_override),
            "--timeout",
            "7",
        ]
    )

    assert parsed.gateway_url == "http://gateway.test"
    assert parsed.worker_url == "http://worker.test"
    assert parsed.mock_memory_llm_url == "http://memory.test"
    assert parsed.compose_file == configured.compose_file.resolve()
    assert parsed.crash_override == configured.crash_override.resolve()
    assert parsed.timeout_seconds == 7


def test_parse_args_rejects_missing_override(tmp_path: Path) -> None:
    configured = options(tmp_path)
    configured.crash_override.unlink()

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--compose-file",
                str(configured.compose_file),
                "--crash-override",
                str(configured.crash_override),
            ]
        )
