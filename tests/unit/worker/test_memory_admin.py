from types import SimpleNamespace

import pytest

from worker import memory_admin


def test_parse_args_supports_init_and_read_only_validation() -> None:
    assert memory_admin.parse_args(["init"]).command == "init"
    assert memory_admin.parse_args(["validate"]).command == "validate"
    assert memory_admin.parse_args(["scope-inventory"]).command == "scope-inventory"
    assert memory_admin.parse_args(["scope-backfill"]).apply is False
    assert memory_admin.parse_args(["scope-backfill", "--apply"]).apply is True


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


async def test_scope_inventory_prints_report_without_writing(monkeypatch, capsys) -> None:
    settings = object()
    plan = SimpleNamespace(unresolved_memory_ids=(), to_report=lambda: {"total_memories": 0})
    calls: list[object] = []

    async def plan_scope(value: object) -> object:
        calls.append(value)
        return plan

    monkeypatch.setattr(memory_admin, "get_worker_settings", lambda: settings)
    monkeypatch.setattr(memory_admin, "plan_scope_backfill", plan_scope)

    assert await memory_admin.run(["scope-inventory"]) == 0
    assert calls == [settings]
    assert '"total_memories": 0' in capsys.readouterr().out


async def test_scope_backfill_dry_run_never_applies(monkeypatch, capsys) -> None:
    settings = object()
    plan = SimpleNamespace(unresolved_memory_ids=(), to_report=lambda: {"total_memories": 3})
    calls: list[bool] = []

    async def plan_scope(_value: object) -> object:
        return plan

    async def apply_scope(_settings, _plan, *, dry_run: bool) -> int:
        calls.append(dry_run)
        return 0

    monkeypatch.setattr(memory_admin, "get_worker_settings", lambda: settings)
    monkeypatch.setattr(memory_admin, "plan_scope_backfill", plan_scope)
    monkeypatch.setattr(memory_admin, "apply_scope_backfill", apply_scope)

    assert await memory_admin.run(["scope-backfill"]) == 0
    assert calls == [True]
    assert "dry-run: updated=0" in capsys.readouterr().out


async def test_scope_backfill_apply_and_unresolved_gate(monkeypatch, capsys) -> None:
    settings = object()
    calls: list[bool] = []

    async def plan_scope(_value: object) -> object:
        return SimpleNamespace(
            unresolved_memory_ids=("memory-1",), to_report=lambda: {"total_memories": 1}
        )

    async def apply_scope(_settings, _plan, *, dry_run: bool) -> int:
        calls.append(dry_run)
        return 2

    monkeypatch.setattr(memory_admin, "get_worker_settings", lambda: settings)
    monkeypatch.setattr(memory_admin, "plan_scope_backfill", plan_scope)
    monkeypatch.setattr(memory_admin, "apply_scope_backfill", apply_scope)

    # Unresolved rows still apply when explicitly forced, but exit non-zero.
    assert await memory_admin.run(["scope-backfill", "--apply"]) == 1
    assert calls == [False]
    assert "applied: updated=2" in capsys.readouterr().out
