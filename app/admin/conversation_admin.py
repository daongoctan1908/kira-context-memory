"""Bounded operator recovery for deletion-pending conversations."""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from enum import IntEnum
from typing import Protocol, TextIO

from pydantic import PostgresDsn, Secret, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreError,
)
from app.infrastructure.postgres import create_postgres_engine
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter


class ConversationAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    database_url: Secret[PostgresDsn]
    memory_schema: str = "memory"
    memory_collection_name: str = "memories"
    postgres_pool_size: int = 2
    postgres_max_overflow: int = 0
    postgres_pool_timeout_seconds: float = 2
    postgres_connect_timeout_seconds: float = 2
    postgres_command_timeout_seconds: float = 10


class ConversationAdminStore(Protocol):
    async def validate_schema(self) -> None: ...

    async def purge_pending_conversations(self, *, limit: int) -> int: ...


class ConversationAdminExitCode(IntEnum):
    SUCCESS = 0
    INTERNAL_ERROR = 1
    USAGE_OR_CONFIGURATION = 2
    DEPENDENCY_ERROR = 3
    INTERRUPTED = 130


class OperatorArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        _write_json({"error": "invalid_invocation"}, sys.stderr)
        raise SystemExit(ConversationAdminExitCode.USAGE_OR_CONFIGURATION)


def build_parser() -> OperatorArgumentParser:
    parser = OperatorArgumentParser(
        prog="kira-conversations",
        description="Recover deletion-pending chatbot conversations",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    purge = commands.add_parser("purge-pending")
    purge.add_argument("--limit", type=_bounded_limit, default=100)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


async def execute_command(
    args: argparse.Namespace,
    store: ConversationAdminStore,
    *,
    output: TextIO | None = None,
) -> ConversationAdminExitCode:
    if args.command != "purge-pending":
        raise ValueError("unsupported conversation admin command")
    purged = await store.purge_pending_conversations(limit=args.limit)
    if isinstance(purged, bool) or not isinstance(purged, int) or not 0 <= purged <= args.limit:
        raise ValueError("invalid purge result")
    _write_json({"limit": args.limit, "purged": purged}, output or sys.stdout)
    return ConversationAdminExitCode.SUCCESS


async def run(
    args: argparse.Namespace,
    *,
    settings: ConversationAdminSettings | None = None,
    engine: AsyncEngine | None = None,
    output: TextIO | None = None,
    engine_factory: Callable[[ConversationAdminSettings], AsyncEngine] | None = None,
    store_factory: Callable[[AsyncEngine, ConversationAdminSettings], ConversationAdminStore]
    | None = None,
) -> ConversationAdminExitCode:
    resolved_settings = settings or ConversationAdminSettings()  # type: ignore[call-arg]
    resolved_engine = engine
    owns_engine = resolved_engine is None
    if resolved_engine is None:
        resolved_engine = (engine_factory or create_postgres_engine)(resolved_settings)
    try:
        if store_factory is None:
            store: ConversationAdminStore = PostgresConversationStoreAdapter(
                resolved_engine,
                memory_enabled=True,
                memory_schema=resolved_settings.memory_schema,
                memory_collection=resolved_settings.memory_collection_name,
            )
        else:
            store = store_factory(resolved_engine, resolved_settings)
        await store.validate_schema()
        return await execute_command(args, store, output=output)
    finally:
        if owns_engine:
            await resolved_engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return int(asyncio.run(run(parse_args(argv))))
    except KeyboardInterrupt:
        _write_error("interrupted")
        return int(ConversationAdminExitCode.INTERRUPTED)
    except (ValueError, ValidationError, ConversationStoreConfigurationError):
        _write_error("invalid_configuration")
        return int(ConversationAdminExitCode.USAGE_OR_CONFIGURATION)
    except (ConversationStoreConnectionError, ConversationStoreError):
        _write_error("conversation_store_unavailable")
        return int(ConversationAdminExitCode.DEPENDENCY_ERROR)
    except Exception:
        _write_error("internal_error")
        return int(ConversationAdminExitCode.INTERNAL_ERROR)


def _bounded_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer") from None
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def _write_json(payload: dict[str, object], output: TextIO) -> None:
    print(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        file=output,
    )


def _write_error(code: str) -> None:
    _write_json({"error": code}, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
