# Week 4 Batch A PostgreSQL memory job foundation acceptance

## Result

Batch A is complete. A normally completed KiRa turn can be persisted with one reference-only
memory job in the same PostgreSQL transaction, and independent Worker processes can safely claim
that durable work through the application queue port. Redis and Redis Stream remain absent.

The delivered tasks are:

| Task | Delivered capability |
| --- | --- |
| T4.1 | ADR selecting PostgreSQL as the only Week 4 asynchronous memory queue |
| T4.2 | Framework-free job models, sanitized errors, and `MemoryJobQueuePort` |
| T4.3 | Versioned `memory_jobs` migration, lifecycle constraints, and partial indexes |
| T4.4 | Atomic completed-turn plus optional job scheduling with stable deduplication |
| T4.5 | PostgreSQL claim, lease, transition, stats, dead-list, requeue, and purge adapter |
| T4.6 | Real PostgreSQL concurrency, ordering, lease, boundary-isolation, and regression gates |

## T4.6 concurrency and lifecycle gates

The real PostgreSQL suite verifies:

- deterministic due ordering by due time, creation time, and event ID;
- two concurrent queue adapters each claim six of twelve jobs, with no duplicate ownership and no
  missing job;
- a locked candidate is skipped immediately while another due job remains claimable;
- each claim has a fresh owner/token/expiry lease and increments the processing attempt;
- retry due time is respected;
- expired leases below the attempt limit are reclaimed with a new token;
- stale, wrong, expired, and already-used lease tokens cannot mutate a job;
- an expired final attempt moves to `dead` without a sixth delivery;
- jobs for different users sharing the same public session ID retain their exact, distinct
  conversation, turn, and assistant-boundary references;
- completed/dead statistics, sanitized dead listing, explicit requeue, and globally bounded
  terminal purge preserve pending and processing rows.

## Batch A transaction and data-minimization gates

The retained T4.3-T4.4 PostgreSQL tests continue to prove that:

- completed user/assistant messages and their optional job commit or roll back together;
- a duplicate completed turn returns the same job event ID and cannot create a second row;
- formation disabled creates no job and does not backfill an older turn;
- failed, partial, empty, disconnected, or identity-less request paths do not create a completed
  turn/job pair;
- `memory_jobs` contains only boundary and operational fields, never conversation text, prompts,
  provider responses, memory facts, credentials, or duplicated user/session IDs;
- foreign keys, lifecycle checks, unique assistant boundary, cascade behavior, and all four partial
  operational indexes are installed at schema revision `20260908_0003`.

## Local evidence

Validated on 2026-09-09:

- queue adapter unit tests: 35 passed;
- full PostgreSQL/pgvector marker: 50 passed, 1 existing opt-in semantic provider gate skipped;
- full default suite: 454 passed, 52 skipped, 93.02% coverage;
- disposable PostgreSQL migration cycle `base -> head -> base -> head` succeeded and the temporary
  database was removed;
- Alembic autogeneration reported no pending schema operations;
- Ruff lint and format checks passed;
- Docker image `kira-context:0.4.0-batch-a` built successfully, runs as non-root user `kira`,
  contains the queue adapter, and contains no importable Redis client;
- the installed environment has no importable Redis client, the application/Worker/Compose files
  contain no Redis reference, and no Redis container is present.

## Boundary after Batch A

Batch A does not run Mem0 from a Worker or add a polling process. Worker-specific settings,
processing classification, concurrent runner lifecycle, health/readiness/metrics, cleanup
scheduling, and operator CLI remain later Week 4 tasks. The Gateway SSE contract is unchanged.
