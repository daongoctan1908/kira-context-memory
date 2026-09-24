"""Administrative entry point for the pgvector memory schema."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.infrastructure.memory.postgres_admin import (
    apply_scope_backfill,
    dump_scope_backfill_report,
    initialize_memory_schema,
    plan_scope_backfill,
    validate_memory_schema,
)
from worker.settings import get_worker_settings


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the KiRa memory schema")
    parser.add_argument(
        "command", choices=("init", "validate", "scope-inventory", "scope-backfill")
    )
    parser.add_argument(
        "--apply", action="store_true", help="write backfill changes (default: dry-run)"
    )
    return parser.parse_args(argv)


async def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_worker_settings()
    if args.command == "init":
        state = await initialize_memory_schema(settings)
        outcome = "ready"
        print(
            f"memory schema {outcome}: version={state.schema_version} dims={state.embedding_dims}"
        )
        return 0
    if args.command == "validate":
        state = await validate_memory_schema(settings)
        outcome = "valid"
        print(
            f"memory schema {outcome}: version={state.schema_version} dims={state.embedding_dims}"
        )
        return 0

    plan = await plan_scope_backfill(settings)
    if args.command == "scope-inventory":
        print(dump_scope_backfill_report(plan))
        return 0

    applied = await apply_scope_backfill(settings, plan, dry_run=not args.apply)
    print(dump_scope_backfill_report(plan))
    print(f"scope backfill {'applied' if args.apply else 'dry-run'}: updated={applied}")
    return 0 if not plan.unresolved_memory_ids else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
