# Week 4 T4.3 PostgreSQL memory job schema

## Delivered checkpoint

T4.3 adds Alembic revision `20260908_0003` and the matching SQLAlchemy Core table for the durable
PostgreSQL memory queue selected in T4.1. It does not enqueue jobs, change completed-turn writes,
claim work, or run a Worker.

Each `memory_jobs` row contains only an event UUID, one unique assistant-boundary message
reference, and operational lifecycle fields. Conversation text, prompts, provider responses,
credentials, memory facts, and user/session identifiers are deliberately absent. Deleting the
owning persisted conversation cascades through its messages to the associated job.

## Persisted lifecycle

New rows default to `pending`, schema version 1, zero attempts/requeues, and an immediately due
`next_attempt_at`. The schema permits only:

- `pending`, with no lease or terminal result;
- `processing`, with a complete owner/token/expiry lease and at least one attempt;
- `completed`, with a completion time, nonnegative lifecycle-event count, and at least one
  attempt;
- `dead`, with a dead time, sanitized error class, and at least one attempt.

Lease fields are all present or all absent according to status. Completion and dead timestamps
are mutually exclusive and cannot precede row creation. Counts cannot be negative. The unique
boundary constraint is the database idempotency guard that T4.4 scheduling will use.

## Indexes

Four partial indexes keep hot paths bounded without indexing irrelevant lifecycle rows:

- due pending claims: `(next_attempt_at, created_at, event_id)`;
- abandoned processing leases: `(lease_expires_at, event_id)`;
- completed retention cleanup: `(completed_at, event_id)`;
- dead retention cleanup: `(dead_at, event_id)`.

The `created_at` plus `event_id` tie-breakers make due-job selection deterministic. Claim SQL and
conditional lease transitions remain T4.5.

## Acceptance evidence

Unit tests lock the exact reference-only columns, named checks, foreign key, unique boundary, and
partial index definitions. PostgreSQL integration tests apply the real Alembic chain and verify
defaults, all legal states, rejected illegal combinations, unique boundaries, cascade behavior,
and the installed partial indexes. `alembic check` must report no pending schema operations.

Local evidence on 2026-09-08:

- targeted PostgreSQL migration/schema plus conversation regression: 33 passed;
- full test suite: 406 passed, 38 skipped, 92.99% coverage;
- Ruff lint and format checks passed;
- Docker migration exited 0 at revision `20260908_0003`; rebuilt Gateway health and readiness
  passed.

Redis remains absent. T4.3 does not change the Gateway public API or runtime behavior.
