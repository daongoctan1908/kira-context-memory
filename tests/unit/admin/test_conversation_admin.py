"""Conversation erasure recovery CLI remains bounded and sanitized."""

import argparse
import json
from io import StringIO

import pytest

import app.admin.conversation_admin as cli
from app.admin.conversation_admin import (
    ConversationAdminExitCode,
    ConversationAdminSettings,
    execute_command,
    parse_args,
)
from app.domain.errors.conversation import ConversationStoreConnectionError


class FakeStore:
    def __init__(self, *, purged: int = 0) -> None:
        self.purged = purged
        self.limits: list[int] = []
        self.validated = False

    async def validate_schema(self) -> None:
        self.validated = True

    async def purge_pending_conversations(self, *, limit: int) -> int:
        self.limits.append(limit)
        return self.purged


async def test_purge_pending_is_bounded_and_machine_readable() -> None:
    output = StringIO()
    store = FakeStore(purged=3)

    result = await execute_command(
        parse_args(["purge-pending", "--limit", "10"]),
        store,
        output=output,
    )

    assert result is ConversationAdminExitCode.SUCCESS
    assert store.limits == [10]
    assert json.loads(output.getvalue()) == {"limit": 10, "purged": 3}


@pytest.mark.parametrize("limit", ("0", "101", "not-a-number"))
def test_parser_rejects_invalid_limits_without_echoing_input(limit, capsys) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(["purge-pending", "--limit", limit])
    assert captured.value.code == int(ConversationAdminExitCode.USAGE_OR_CONFIGURATION)
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_invocation"}


@pytest.mark.parametrize(
    ("error", "code", "payload"),
    (
        (ValueError("private detail"), 2, "invalid_configuration"),
        (ConversationStoreConnectionError(), 3, "conversation_store_unavailable"),
        (RuntimeError("private detail"), 1, "internal_error"),
    ),
)
def test_main_sanitizes_failures(monkeypatch, capsys, error, code, payload) -> None:
    async def fail(_args: argparse.Namespace):
        raise error

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["purge-pending"]) == code
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"error": payload}
    assert "private" not in captured.err


async def test_run_validates_and_disposes_only_owned_engine() -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    stores: list[FakeStore] = []

    def store_factory(_engine, _settings):
        store = FakeStore()
        stores.append(store)
        return store

    settings = ConversationAdminSettings(
        _env_file=None,
        database_url="postgresql://operator:secret@db/kira",
    )
    owned = FakeEngine()
    assert (
        await cli.run(
            parse_args(["purge-pending"]),
            settings=settings,
            output=StringIO(),
            engine_factory=lambda _settings: owned,  # type: ignore[arg-type]
            store_factory=store_factory,  # type: ignore[arg-type]
        )
        is ConversationAdminExitCode.SUCCESS
    )
    assert owned.disposed is True
    assert stores[-1].validated is True

    injected = FakeEngine()
    await cli.run(
        parse_args(["purge-pending"]),
        settings=settings,
        engine=injected,  # type: ignore[arg-type]
        output=StringIO(),
        store_factory=store_factory,  # type: ignore[arg-type]
    )
    assert injected.disposed is False
