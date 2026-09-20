"""Administrative entry point for the pgvector memory schema."""

import argparse
import asyncio
from collections.abc import Sequence

from app.infrastructure.memory.postgres_admin import (
    initialize_memory_schema,
    validate_memory_schema,
)
from worker.settings import get_worker_settings


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the KiRa memory schema")
    parser.add_argument("command", choices=("init", "validate"))
    return parser.parse_args(argv)


async def run(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_worker_settings()
    if args.command == "init":
        state = await initialize_memory_schema(settings)
        outcome = "ready"
    else:
        state = await validate_memory_schema(settings)
        outcome = "valid"
    print(f"memory schema {outcome}: version={state.schema_version} dims={state.embedding_dims}")


if __name__ == "__main__":
    asyncio.run(run())
