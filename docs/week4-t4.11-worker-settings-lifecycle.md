# Week 4 T4.11 — Worker settings and lifecycle dependencies

## Outcome

T4.11 establishes the memory Worker's process boundary. It deliberately does not start a poller,
claim a job, execute memory formation, expose HTTP routes, or implement retry/cleanup behavior.
Those behaviors begin in T4.12.

The Worker has its own immutable `WorkerSettings`. It requires only:

- the application PostgreSQL queue/conversation database;
- the Mem0 PostgreSQL/pgvector store;
- the embedding and memory-formation LLM providers used by native Mem0 V3;
- bounded queue, lease, shutdown, metrics, and retention controls.

It has no KiRa authentication, KiRa transport, or query-rewriter `VLLM_*` configuration. The
`MEMORY_LLM_*` settings are a separate Mem0 formation dependency and remain required.

## Startup contract

`worker_dependency_lifespan()` performs these steps in order:

1. Load and validate `WorkerSettings`.
2. Create the Worker-owned async PostgreSQL pool, unless one is injected by a test.
3. Validate the exact Alembic revision through both the conversation and memory-job adapters.
4. Read-only validate the Mem0 schema metadata, installed custom Mem0 version, pgvector version,
   embedding model/dimension, and both configured vector tables.
5. Construct the Worker-owned `Mem0Adapter`, unless a test double is injected.
6. Construct `ProcessMemoryUseCase` with the bounded exact-boundary message limit.
7. Yield validated dependencies to the later runner/application layer.

Missing or malformed configuration, an unexpected application migration, an incompatible memory
schema, or invalid Mem0 construction prevents startup. A runtime dependency outage is surfaced as
its existing sanitized typed error; readiness/degraded behavior is added with the Worker HTTP app
in T4.14.

The lifecycle closes only resources it creates. An owned Mem0 adapter is closed before the owned
PostgreSQL engine is disposed, including partial-startup failures. Injected resources stay owned by
their caller.

## Invariants

- `MEMORY_JOB_LEASE_SECONDS` must be strictly greater than
  `MEMORY_OPERATION_TIMEOUT_SECONDS + CONVERSATION_OPERATION_TIMEOUT_SECONDS`.
- Retry delays contain exactly `MEMORY_JOB_MAX_ATTEMPTS - 1` finite positive values.
- The default provider-attempt budget is five, with delays `1, 5, 30, 120` seconds.
- Default batch size is 10 and default execution concurrency is 4.
- Default lease is 120 seconds; graceful shutdown budget is 30 seconds.
- Completed jobs retain for 7 days and dead jobs for 30 days by default.
- Memory formation reads an even, complete-turn boundary capped at 10 messages by default.
- Secrets remain Pydantic secret values and are not rendered in settings representations.

## Acceptance evidence

Unit tests cover:

- defaults and environment parsing without any KiRa/query-rewriter setting;
- required PostgreSQL, embedding, and memory-LLM inputs;
- secret redaction, retry cardinality/value validation, pool bounds, and lease safety;
- successful dependency construction and ownership-aware shutdown;
- fail-fast application schema, queue schema, memory schema, and Mem0 construction paths;
- no job processing as a side effect of entering the lifecycle.

The existing PostgreSQL integration schema test also validates the read-only memory schema probe
against a real pgvector database and rejects a mismatched embedding model. Full repository tests,
Ruff, Docker build/import smoke, and existing PostgreSQL gates remain regression requirements.
