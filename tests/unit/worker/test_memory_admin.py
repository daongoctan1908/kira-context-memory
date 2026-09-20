from types import SimpleNamespace

import pytest

from worker import memory_admin


def test_parse_args_supports_init_and_read_only_validation() -> None:
    assert memory_admin.parse_args(["init"]).command == "init"
    assert memory_admin.parse_args(["validate"]).command == "validate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_call", "expected_output"),
    [
        ("init", "init", "memory schema ready: version=3 dims=1024"),
        ("validate", "validate", "memory schema valid: version=3 dims=1024"),
    ],
)
async def test_memory_admin_commands_use_the_expected_schema_operation(
    monkeypatch,
    capsys,
    command: str,
    expected_call: str,
    expected_output: str,
) -> None:
    settings = object()
    calls: list[tuple[str, object]] = []
    state = SimpleNamespace(schema_version=3, embedding_dims=1024)

    async def initialize(value: object) -> object:
        calls.append(("init", value))
        return state

    async def validate(value: object) -> object:
        calls.append(("validate", value))
        return state

    monkeypatch.setattr(memory_admin, "get_worker_settings", lambda: settings)
    monkeypatch.setattr(memory_admin, "initialize_memory_schema", initialize)
    monkeypatch.setattr(memory_admin, "validate_memory_schema", validate)

    await memory_admin.run([command])

    assert calls == [(expected_call, settings)]
    assert capsys.readouterr().out.strip() == expected_output
