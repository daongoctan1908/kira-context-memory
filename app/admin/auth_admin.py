"""Sanitized operator CLI for application-managed users and sessions."""

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Callable, Sequence
from enum import IntEnum
from typing import TextIO

from pydantic import PostgresDsn, Secret
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.services.auth import normalize_username
from app.application.services.auth_admin import AuthAdminService
from app.domain.errors.auth import AuthConflictError, AuthError, AuthStoreError
from app.infrastructure.auth import PwdlibPasswordHasher
from app.infrastructure.postgres import PostgresAuthStore, create_postgres_engine


class AuthAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    database_url: Secret[PostgresDsn]
    postgres_pool_size: int = 2
    postgres_max_overflow: int = 0
    postgres_pool_timeout_seconds: float = 2
    postgres_connect_timeout_seconds: float = 2
    postgres_command_timeout_seconds: float = 5


class AuthAdminExitCode(IntEnum):
    SUCCESS = 0
    INTERNAL_ERROR = 1
    USAGE_OR_CONFIGURATION = 2
    DEPENDENCY_ERROR = 3
    NOT_FOUND = 4
    INTERRUPTED = 130


class OperatorArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        _write_json({"error": "invalid_invocation"}, sys.stderr)
        raise SystemExit(AuthAdminExitCode.USAGE_OR_CONFIGURATION)


def build_parser() -> OperatorArgumentParser:
    parser = OperatorArgumentParser(prog="kira-auth-admin", description="Manage chatbot users")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "reset-password"):
        command = commands.add_parser(name)
        command.add_argument("--username", required=True)
        command.add_argument("--password-stdin", action="store_true")
    for name in ("disable", "enable", "revoke-sessions"):
        command = commands.add_parser(name)
        command.add_argument("--username", required=True)
    list_command = commands.add_parser("list")
    list_command.add_argument("--limit", type=_bounded_limit, default=100)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


async def execute_command(
    args: argparse.Namespace,
    service: AuthAdminService,
    *,
    output: TextIO | None = None,
    stdin: TextIO | None = None,
    password_prompt: Callable[[str], str] = getpass.getpass,
) -> AuthAdminExitCode:
    target = output or sys.stdout
    username = normalize_username(args.username) if hasattr(args, "username") else None
    if args.command == "create":
        password = _read_password(args, stdin=stdin, password_prompt=password_prompt)
        assert username is not None
        user_id = await service.create_user(username, password)
        _write_json({"created": True, "user_id": str(user_id), "username": username}, target)
        return AuthAdminExitCode.SUCCESS
    if args.command == "reset-password":
        password = _read_password(args, stdin=stdin, password_prompt=password_prompt)
        assert username is not None
        changed = await service.reset_password(username, password)
        _write_json({"password_reset": changed, "username": username}, target)
        return AuthAdminExitCode.SUCCESS if changed else AuthAdminExitCode.NOT_FOUND
    if args.command in {"disable", "enable"}:
        assert username is not None
        enabled = args.command == "enable"
        found = await service.set_enabled(username, enabled=enabled)
        _write_json({"enabled": enabled if found else None, "username": username}, target)
        return AuthAdminExitCode.SUCCESS if found else AuthAdminExitCode.NOT_FOUND
    if args.command == "revoke-sessions":
        assert username is not None
        count = await service.revoke_sessions(username)
        _write_json({"revoked_sessions": count, "username": username}, target)
        return AuthAdminExitCode.SUCCESS if count is not None else AuthAdminExitCode.NOT_FOUND
    if args.command == "list":
        users = await service.list_users(limit=args.limit)
        _write_json(
            {
                "count": len(users),
                "items": [
                    {
                        "enabled": user.enabled,
                        "failed_login_count": user.failed_login_count,
                        "locked_until": user.locked_until.isoformat()
                        if user.locked_until
                        else None,
                        "user_id": str(user.user_id),
                        "username": user.username,
                    }
                    for user in users
                ],
                "limit": args.limit,
            },
            target,
        )
        return AuthAdminExitCode.SUCCESS
    raise ValueError("unsupported auth admin command")


async def run(
    args: argparse.Namespace,
    *,
    settings: AuthAdminSettings | None = None,
    engine: AsyncEngine | None = None,
    output: TextIO | None = None,
    stdin: TextIO | None = None,
    password_prompt: Callable[[str], str] = getpass.getpass,
    engine_factory: Callable[[AuthAdminSettings], AsyncEngine] | None = None,
) -> AuthAdminExitCode:
    resolved_settings = settings or AuthAdminSettings()  # type: ignore[call-arg]
    resolved_engine = engine
    owns_engine = resolved_engine is None
    if resolved_engine is None:
        resolved_engine = (engine_factory or create_postgres_engine)(resolved_settings)
    try:
        store = PostgresAuthStore(resolved_engine)
        await store.validate_schema()
        service = AuthAdminService(store, PwdlibPasswordHasher())
        return await execute_command(
            args,
            service,
            output=output,
            stdin=stdin,
            password_prompt=password_prompt,
        )
    finally:
        if owns_engine:
            await resolved_engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return int(asyncio.run(run(parse_args(argv))))
    except KeyboardInterrupt:
        _write_error("interrupted")
        return int(AuthAdminExitCode.INTERRUPTED)
    except (ValueError, AuthConflictError):
        _write_error("invalid_request")
        return int(AuthAdminExitCode.USAGE_OR_CONFIGURATION)
    except AuthStoreError:
        _write_error("auth_store_unavailable")
        return int(AuthAdminExitCode.DEPENDENCY_ERROR)
    except AuthError:
        _write_error("auth_operation_failed")
        return int(AuthAdminExitCode.USAGE_OR_CONFIGURATION)
    except Exception:
        _write_error("internal_error")
        return int(AuthAdminExitCode.INTERNAL_ERROR)


def _read_password(
    args: argparse.Namespace,
    *,
    stdin: TextIO | None,
    password_prompt: Callable[[str], str],
) -> str:
    if args.password_stdin:
        value = (stdin or sys.stdin).readline().rstrip("\r\n")
    else:
        value = password_prompt("Password: ")
    if not value:
        raise ValueError("password must not be empty")
    return value


def _bounded_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer") from None
    if not 1 <= parsed <= 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return parsed


def _write_json(payload: dict[str, object], output: TextIO) -> None:
    print(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True), file=output
    )


def _write_error(code: str) -> None:
    _write_json({"error": code}, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
