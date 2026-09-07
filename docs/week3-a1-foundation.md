# Week 3 Batch A1 — identity, pgvector, and Mem0 adapter foundation

## Delivered boundary

- Trusted identity enters application code through `IdentityPort`. KiRa service-account
  authentication is not end-user identity. The only concrete baseline provider is a static
  dev/test adapter; it is rejected in production configuration.
- Missing identity is availability-first but data-safe: `/chat` sends only the current query to
  KiRa and does not read, rewrite from, or persist conversation context.
- PostgreSQL conversations are unique per `(user_id, session_id)`. Migration `20260906_0002`
  isolates pre-existing conversations under synthetic `legacy:<conversation_id>` owners.
- A completed transactional append returns `conversation_id`, `turn_id`, and its exact assistant
  `boundary_message_id`. `read_through_boundary` verifies ownership and returns an old-to-new
  bounded snapshot ending at that boundary.
- `LongTermMemoryPort` keeps Mem0 outside domain/application. `Mem0Adapter` enforces a `user_id`
  filter on search and rejects any result whose owner does not match.
- Formation calls pristine Mem0 v2.0.20 V3 `add(..., infer=True)` only. The adapter has no update
  or delete path and rejects non-`ADD` result events. Taxonomy and custom extraction policy are
  intentionally deferred to Batch B.

## Database ownership

Local Compose pins `pgvector/pgvector:0.8.6-pg16-bookworm`. Memory objects live in the `memory`
schema of the configured PostgreSQL database. Gateway and future worker runtime use the Mem0
provider with `auto_create=false`; they never create extensions, schemas, tables, or indexes.

An administrative command owns initialization:

```powershell
uv run python -m worker.memory_admin init
```

It performs these operations transactionally:

1. Probe the OpenAI-compatible `/v1/embeddings` endpoint without requesting an artificial
   dimension and require the returned vector length to equal `MEMORY_EMBEDDING_DIMS`.
2. Acquire an advisory transaction lock and ensure the `vector` extension and configured schema.
3. Create the main and entity collections with matching `vector(<dims>)` columns.
4. Create HNSW cosine, user-scope, and text-search indexes.
5. Persist and validate schema version, embedding model/dimension, internal Mem0 version, and
   pgvector extension version. A mismatch fails closed and requires an explicit migration.

Use a privileged `MEMORY_ADMIN_DATABASE_URL` for the command. Use a DML-only
`MEMORY_DATABASE_URL` for the runtime adapter. Secrets, prompts, messages, and endpoint details
must not be logged. The container sets `MEM0_TELEMETRY=False` and uses writable ephemeral
`MEM0_DIR=/tmp/kira-mem0`; PostgreSQL remains the persistent memory store.

## Deferred work

Batch A1 does not wire long-term retrieval or formation into `/chat`, does not start a worker or
queue, and does not add memory taxonomy/extraction rules. Cross-session recall and fallback
observability remain later Week 3 checkpoints.

## Local checkpoint evidence — 2026-09-07

- Ruff lint and format checks pass.
- Full suite: 268 passed, 16 environment-gated PostgreSQL tests skipped; coverage 91.17%.
- PostgreSQL/pgvector suite against the pinned container: 16 passed, including user/session
  isolation, exact boundary, migration revision, and memory schema initialization.
- Internal Mem0 pgvector provider suite: 91 passed; runtime `auto_create=false` does not issue DDL.
- Docker image `kira-context:0.3.0` builds and runs as UID 10001 with
  `viettel-mem0==2.0.20+viettel.2`; Redis SDK is absent.
- Admin-init ran from the built image against pgvector 0.8.6 and the synthetic embedding stub;
  schema version 1 and dimension 3 were verified.
- Week 2 socket smoke remains green for all eight rewrite cases. Real KiRa, Qwen memory LLM, and
  embedding deployment gates are not run without approved internal endpoints and credentials.
