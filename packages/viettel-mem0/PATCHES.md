# Viettel patch ledger

The pristine upstream baseline is commit `4c95a43`. This ledger describes every
intentional delta after that point.

## Packaging delta: `2.0.20+viettel.1`

- Rename the Python distribution from `mem0ai` to `viettel-mem0`.
- Keep the upstream Python import namespace `mem0` unchanged.
- Resolve `mem0.__version__` from the internal distribution name.
- Add a narrow `postgres` optional dependency containing psycopg3 and its pool.
- Register the distribution as a local path source in the parent uv project.

No memory extraction, search, deduplication, update, or deletion logic is
changed in this patch. In particular, the V3 formation pipeline remains
ADD-only. The parent application does not install the upstream broad
`vector-stores` extra, which contains Redis and unrelated providers.

The fork intentionally is not a uv workspace member. uv resolves every optional
dependency group of workspace members into the shared lock, which would put
Redis, GPU runtimes, and unrelated vector stores into the Gateway lock even
though they are not installed. The local path source keeps the same in-repo
development model while locking only the `postgres` extra selected by the
Gateway.

## PostgreSQL ownership boundary: `2.0.20+viettel.2`

- Add an explicit PostgreSQL `schema_name` to the pgvector provider.
- Add `auto_create=false` so Gateway/worker runtime processes fail closed when
  the memory schema has not been initialized and reject explicit create/delete/reset
  collection operations instead of issuing DDL.
- Qualify collection SQL with the configured schema and scope discovery to that
  schema.
- Expose deterministic pgvector pool cleanup so the KiRa adapter can close all
  runtime resources rather than relying on Python finalization.

Schema and extension creation remain available only when `auto_create=true` for
backward compatibility. KiRa configures `auto_create=false`; its separate admin
initializer owns extension, schema, table, and index DDL. Memory formation stays
the pristine upstream V3 ADD-only pipeline.

## Event-scoped formation receipts: `2.0.20+viettel.3`

- Accept an optional `formation_event_id` on the asynchronous add path.
- Check a collection-scoped PostgreSQL receipt before provider calls.
- Commit vector rows and the receipt in one transaction, and return the first committed result on
  replay.

This makes at-least-once Worker delivery idempotent without changing extraction policy. The
application-owned memory initializer creates and validates the receipt table.

## Dependency-free observation hook: `2.0.20+viettel.4`

- Add an optional, request-local observer protocol with a no-op default and fail-open callbacks.
- Observe receipt, existing-memory search, extraction, parse, embedding, deduplication, and
  persistence stages without importing OpenTelemetry or Langfuse into the vendored package.
- Expose model and token usage before provider adapters reduce responses to strings or vectors.
- Preserve observer context across `asyncio.to_thread` and isolate concurrent requests with
  `ContextVar` bindings.

Observer-enabled and observer-disabled tests assert identical provider requests, returned facts,
memory rows, and receipts. Because this patch changes no persisted contract, the `.4` package keeps
the `.3` schema-contract marker; no metadata, vector schema, receipt, or stored-memory migration is
required, and the Phase 3 runtime remains rollback-compatible.

## Product history and receipt ownership: `2.0.20+viettel.5`

- Add an explicit `MemoryConfig.history_enabled` switch. It defaults to `true`, preserving native
  Mem0 behavior; the KiRa product runtime sets it to `false` because PostgreSQL is the authoritative
  bounded transcript and lifecycle source.
- Require `conversation_id` for event-scoped formation and persist it on every receipt, including
  empty/deduplicated formations.
- Validate `event_id + user_id + conversation_id` together on receipt replay and atomic insert.

The extraction, embedding, hash deduplication, entity linking, and returned lifecycle semantics are
unchanged. This is a persisted receipt-contract change: application schema version 3 backfills the
owner from vector payloads or durable memory jobs and refuses to guess unresolved ownership.

## Active conversation ownership fence: `2.0.20+viettel.6`

- Add an opt-in PostgreSQL ownership fence; the native default is disabled.
- Filter semantic, keyword, and list reads to memories whose payload owner matches an active
  authoritative conversation.
- Lock the active conversation row with `FOR KEY SHARE` in the vector/receipt transaction so a
  concurrent deletion mark using `FOR UPDATE` is serialized with late formation writes.
- Revalidate entity links in their own write transaction and drop missing, cross-user, or inactive
  memory IDs so entity side effects cannot recreate orphan links after deletion.

The memory table and receipt shape remain schema version 3. Existing `.5` schema metadata upgrades
in place to `.6`; no vector, receipt, or application conversation data is rewritten.
