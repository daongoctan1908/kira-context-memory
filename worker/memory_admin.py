"""Administrative entry point for the pgvector memory schema."""

import argparse
import asyncio

from app.config.settings import get_settings
from app.infrastructure.memory.postgres_admin import initialize_memory_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the KiRa memory schema")
    parser.add_argument("command", choices=("init",))
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    if args.command == "init":
        state = await initialize_memory_schema(get_settings())
        print(f"memory schema ready: version={state.schema_version} dims={state.embedding_dims}")


if __name__ == "__main__":
    asyncio.run(run())
