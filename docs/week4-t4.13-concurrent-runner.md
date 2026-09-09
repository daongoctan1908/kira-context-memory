# Week 4 T4.13 — Concurrent memory-job runner

## Outcome

`MemoryJobRunner` is the one-shot asynchronous runtime that connects PostgreSQL claims to
`ProcessMemoryJobUseCase`. T4.13 does not expose HTTP endpoints, Prometheus metrics, cleanup, or an
operator CLI; those remain T4.14–T4.16.

## Capacity and claim behavior

Before every claim the runner computes:

```text
free slots = configured concurrency - current in-flight count
claim limit = min(configured batch size, free slots)
```

It never claims while no slot is free. When concurrency is larger than the batch cap, it performs
additional bounded claims until capacity is full. One stable random `lease_owner` identifies the
runner instance; every claim passes the configured lease duration and maximum attempt count.

PostgreSQL remains responsible for due ordering, `FOR UPDATE SKIP LOCKED`, fresh lease tokens, and
expired-lease reclamation. A reclaimed job is processed exactly like a new claim and keeps its
incremented attempt count. At-least-once behavior remains explicit.

## Database failure and backoff

Each claim has the configured database deadline. Timeout is converted to the sanitized queue
connection error and the in-progress database await is cancelled. All typed queue failures enter
an interruptible exponential backoff:

```text
poll interval × 2^(consecutive failures - 1), capped at max(30 seconds, poll interval)
```

A successful poll, including an empty result, immediately resets the failure count and backoff.
The runner records only database availability, last successful poll time, consecutive failure
count, current backoff, and in-flight count. T4.14 will use this snapshot for readiness and metrics.

## Per-job isolation

Provider, boundary, and unexpected runtime failures are classified by `ProcessMemoryJobUseCase`
and transition to completed, retry, or bounded retry/dead. If the queue transition itself fails or
the lease is stale, the per-job task logs only a sanitized error class and ends without attempting
a second transition. The lease is then eligible for PostgreSQL reclamation.

No log or snapshot includes event, user, session, conversation, turn, boundary, query, prompt,
memory, provider payload, credential, or exception message.

## Graceful shutdown

`request_stop()` makes the polling wait interruptible and prevents the next claim. Already claimed
jobs receive `MEMORY_JOB_SHUTDOWN_GRACE_SECONDS` to finish and persist their transitions. When the
grace period expires, remaining tasks are cancelled and awaited. Cancellation propagates through
the processing use case without recording retry/completion/dead, leaving the current database
lease to expire and be reclaimed safely.

External cancellation of the runner follows the same drain behavior and is re-raised after cleanup.
The runner is intentionally single-use so a stopped event and lease-owner identity cannot be
accidentally reused.

## Acceptance evidence

Validated locally on 2026-09-09:

- bounded concurrency, repeated batch claims, lease contract, DB timeout/backoff/recovery,
  per-job isolation, graceful drain, grace expiry cancellation, external cancellation, config
  validation, and single-use tests passed;
- real PostgreSQL acceptance reclaimed an artificially expired lease at attempt 2, read the exact
  completed-turn boundary, processed it once, and stored the completed lifecycle-event count;
- full default suite: 535 passed, 60 skipped, 92.71% coverage;
- full PostgreSQL/pgvector marker: 58 passed, 1 existing opt-in semantic gate skipped;
- Ruff lint and format checks passed;
- Docker image `kira-context:0.4.0-t4.13` built with the expected version label and non-root
  `kira` user; Worker settings, dependency graph, and runner imports passed inside the image with
  the default concurrency of 4.
