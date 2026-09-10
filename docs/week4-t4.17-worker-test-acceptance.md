# Week 4 T4.17 Worker Test Acceptance

## Outcome

T4.17 closes Batch C with an explicit unit/integration gate for the asynchronous memory Worker.
The gate exercises the real PostgreSQL conversation and memory-job adapters while replacing only
the external Mem0/provider boundary with deterministic in-process doubles. It does not weaken the
lease, exact-boundary, retry, retention, readiness, metrics, or operator contracts.

The new cross-component acceptance suite starts the actual `MemoryJobRunner` and Worker FastAPI
runtime against PostgreSQL. It proves both a retry-to-success lifecycle and graceful cancellation
followed by lease reclaim. Existing focused tests remain authoritative for terminal dead-letter,
five-attempt exhaustion, bounded cleanup, and the operator CLI.

## Acceptance matrix

| Requirement | Acceptance evidence |
| --- | --- |
| Retry | `test_memory_worker_acceptance.py` runs a real HTTP Worker runtime; a retryable provider timeout returns the row to `pending`, the runner claims it again, and attempt 2 completes. Metrics contain exactly one retry and one success. |
| Dead | `test_memory_job_processing.py` sends a permanent protocol error directly to `dead`; `test_memory_job_retention.py` proves a retryable error becomes `dead` on the exact fifth configured attempt. |
| Lost lease | `test_memory_worker_acceptance.py` gives a replacement worker a reclaimed lease and proves the cancelled worker's stale token cannot complete the row. |
| Cancellation | The first real runner exceeds its shutdown grace, cancels in-flight formation without writing a synthetic transition, and leaves the job `processing` for expiry/reclaim. |
| Cleanup | `test_memory_job_retention.py` runs `MemoryJobCleanupRunner` against PostgreSQL, deletes only expired terminal rows, preserves recent terminal rows plus all `pending`/`processing` rows, and records bounded metrics. |
| Readiness | The new HTTP runtime acceptance waits for `/ready=200` only after the runner and cached queue snapshot are healthy. Unit tests additionally cover initial 503, DB failure, stale snapshot, timeout, recovery, and stopped runner. `/health` stays 200 while contextual processing is degraded. |
| CLI | `test_memory_job_admin.py` creates a real dead job, verifies sanitized stats/dead listing, requeues it exactly once, rejects a second requeue, and verifies the persisted reset state. |

The matrix also retains the queue/schema/concurrency integration tests from T4.1–T4.10. In
particular, `SKIP LOCKED`, deterministic claim order, final-attempt expiry, idempotent scheduling,
atomic conversation/job persistence, exact user/session boundary ownership, and schema revision
validation remain part of the PostgreSQL marker gate.

## Cross-component scenarios added in T4.17

### Retry, readiness, and metrics

One completed user/assistant turn is atomically persisted with a memory job. The Worker app then:

1. claims attempt 1 from PostgreSQL;
2. receives a typed retryable timeout from the memory boundary;
3. persists the configured retry schedule and releases the lease;
4. claims attempt 2 and rereads the exact persisted boundary;
5. completes with one native lifecycle event;
6. exposes healthy `/health`, `/ready`, and cached Prometheus outcomes.

Both attempts receive the same trusted `CompletedTurnReference` and the exact user/assistant
message pair. No identity, transcript, prompt, event ID, or lease token is added as a metric label.

### Cancellation, lease reclaim, and fencing

A Worker claims a job and blocks inside formation. Shutdown stops new claims, waits for its grace
period, then cancels the in-flight task. The database row remains `processing` with attempt 1 and
its original lease token. After simulated lease expiry, a replacement Worker claims attempt 2 as
`reclaimed`. While that new lease is active, completion with the stale token raises
`MemoryJobLeaseLostError`; the replacement lease alone can complete the row.

This verifies at-least-once delivery and lease-token fencing together. It deliberately does not
claim exactly-once provider side effects.

## Commands and evidence

Validated locally on 2026-09-10 with Docker PostgreSQL healthy:

```powershell
uv run ruff check .
uv run ruff format --check .
uv lock --check
git diff --check

uv run pytest tests/unit/worker --no-cov

$env:POSTGRES_TEST_URL="postgresql+asyncpg://<user>:<password>@127.0.0.1:5432/<database>"
uv run pytest tests/integration/postgres/test_memory_job_processing.py `
  tests/integration/postgres/test_memory_job_runner.py `
  tests/integration/postgres/test_memory_job_retention.py `
  tests/integration/postgres/test_memory_job_admin.py `
  tests/integration/postgres/test_memory_worker_acceptance.py --no-cov
uv run pytest -m postgres_integration --no-cov
Remove-Item Env:POSTGRES_TEST_URL

uv run pytest
```

Results:

- Worker unit suite: 99 passed;
- focused Worker PostgreSQL lifecycle suite: 9 passed;
- full PostgreSQL marker: 63 passed, 1 opt-in semantic-provider test skipped;
- full default suite: 588 passed, 65 skipped, 93.12% coverage;
- Ruff lint/format, lock verification, and Git whitespace checks passed.

All test conversations and their cascaded jobs are removed after each scenario. Test failures do
not leave runner tasks alive, and no real user content or credential is captured as evidence.

## Scope boundary

T4.17 does not call a real memory LLM or embedding service, assert semantic extraction quality,
promise exactly-once provider execution, or add Docker Compose Worker deployment wiring. The
native Mem0 semantic gates from Week 3 remain separate opt-in tests. Compose service wiring and
the final deployed Gateway-to-Worker smoke belong to T4.18 and later deployment tasks.
