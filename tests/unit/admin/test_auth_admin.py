"""Auth admin CLI tests with no database or secret output."""

import argparse
import json
from datetime import UTC, datetime
from io import StringIO
from uuid import UUID

import pytest

import app.admin.auth_admin as cli
from app.admin.auth_admin import AuthAdminExitCode, execute_command, parse_args
from app.domain.errors.auth import AuthStoreError
from app.domain.models.auth import AuthUserSummary

_USER_ID = UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime(2026, 9, 19, tzinfo=UTC)


class FakeService:
    def __init__(self) -> None:
        self.calls = []
        self.found = True

    async def create_user(self, username, password):
        self.calls.append(("create", username, password))
        return _USER_ID

    async def reset_password(self, username, password):
        self.calls.append(("reset", username, password))
        return self.found

    async def set_enabled(self, username, *, enabled):
        self.calls.append(("enabled", username, enabled))
        return self.found

    async def revoke_sessions(self, username):
        self.calls.append(("revoke", username))
        return 3 if self.found else None

    async def list_users(self, *, limit):
        self.calls.append(("list", limit))
        return (AuthUserSummary(_USER_ID, "alice", True, 0, None, _NOW, _NOW),)


async def test_create_reads_password_from_stdin_without_rendering_it() -> None:
    output = StringIO()
    service = FakeService()
    result = await execute_command(
        parse_args(["create", "--username", "alice", "--password-stdin"]),
        service,  # type: ignore[arg-type]
        output=output,
        stdin=StringIO("private-password\n"),
    )

    assert result is AuthAdminExitCode.SUCCESS
    assert service.calls == [("create", "alice", "private-password")]
    assert json.loads(output.getvalue()) == {
        "created": True,
        "user_id": str(_USER_ID),
        "username": "alice",
    }
    assert "private-password" not in output.getvalue()


async def test_reset_uses_hidden_prompt_and_all_operator_commands_are_bounded() -> None:
    service = FakeService()
    output = StringIO()
    await execute_command(
        parse_args(["reset-password", "--username", "alice"]),
        service,  # type: ignore[arg-type]
        output=output,
        password_prompt=lambda _prompt: "replacement-password",
    )
    assert service.calls[-1] == ("reset", "alice", "replacement-password")
    assert "replacement-password" not in output.getvalue()

    for argv in (
        ["disable", "--username", "alice"],
        ["enable", "--username", "alice"],
        ["revoke-sessions", "--username", "alice"],
        ["list", "--limit", "25"],
    ):
        assert (
            await execute_command(
                parse_args(argv),
                service,
                output=StringIO(),  # type: ignore[arg-type]
            )
            is AuthAdminExitCode.SUCCESS
        )
    assert service.calls[-1] == ("list", 25)


async def test_missing_user_and_empty_password_have_stable_outcomes() -> None:
    service = FakeService()
    service.found = False
    for argv in (
        ["reset-password", "--username", "missing", "--password-stdin"],
        ["disable", "--username", "missing"],
        ["revoke-sessions", "--username", "missing"],
    ):
        result = await execute_command(
            parse_args(argv),
            service,  # type: ignore[arg-type]
            output=StringIO(),
            stdin=StringIO("valid-password\n"),
        )
        assert result is AuthAdminExitCode.NOT_FOUND

    with pytest.raises(ValueError, match="must not be empty"):
        await execute_command(
            parse_args(["create", "--username", "alice", "--password-stdin"]),
            service,  # type: ignore[arg-type]
            output=StringIO(),
            stdin=StringIO(""),
        )


@pytest.mark.parametrize(
    "argv",
    (
        [],
        ["create", "--username", "alice", "--password", "secret"],
        ["list", "--limit", "0"],
        ["list", "--limit", "1001"],
    ),
)
def test_parser_rejects_unbounded_or_command_line_password_input(argv, capsys) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(argv)
    assert captured.value.code == int(AuthAdminExitCode.USAGE_OR_CONFIGURATION)
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_invocation"}
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "code", "payload"),
    (
        (ValueError("private password"), 2, "invalid_request"),
        (AuthStoreError("private database"), 3, "auth_store_unavailable"),
        (RuntimeError("private detail"), 1, "internal_error"),
    ),
)
def test_main_sanitizes_all_failures(monkeypatch, capsys, error, code, payload) -> None:
    async def fail(_args: argparse.Namespace):
        raise error

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["list"]) == code
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"error": payload}
    assert "private" not in captured.err


async def test_run_validates_schema_and_disposes_only_owned_engine(monkeypatch) -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class FakeStore:
        def __init__(self, _engine) -> None:
            self.validated = False

        async def validate_schema(self) -> None:
            self.validated = True

        async def list_users(self, *, limit):
            del limit
            return ()

    class FakeHasher:
        pass

    monkeypatch.setattr(cli, "PostgresAuthStore", FakeStore)
    monkeypatch.setattr(cli, "PwdlibPasswordHasher", FakeHasher)
    owned = FakeEngine()
    settings = cli.AuthAdminSettings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:password@db/kira",
    )
    result = await cli.run(
        parse_args(["list"]),
        settings=settings,
        output=StringIO(),
        engine_factory=lambda _settings: owned,  # type: ignore[arg-type]
    )
    assert result is AuthAdminExitCode.SUCCESS
    assert owned.disposed

    injected = FakeEngine()
    await cli.run(
        parse_args(["list"]),
        settings=settings,
        engine=injected,  # type: ignore[arg-type]
        output=StringIO(),
    )
    assert not injected.disposed
