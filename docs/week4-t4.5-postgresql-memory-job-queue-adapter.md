# Week 4 T4.5 PostgreSQL memory job queue adapter

## Delivered checkpoint

T4.5 implements `MemoryJobQueuePort` with SQLAlchemy Core and PostgreSQL. It completes the durable
queue storage boundary needed by the later Worker without starting a polling loop, invoking Mem0,
or changing the Gateway request path.

The adapter validates the exact Alembic revision before Worker startup and maps database failures
to the sanitized queue error contract. Application and domain modules remain independent from
SQLAlchemy, asyncpg, and PostgreSQL types.

## Claim and lease semantics

`claim_due()` selects a bounded deterministic batch containing either:

- pending jobs whose `next_attempt_at` is due; or
- processing jobs whose lease has expired.

Pending rows at the configured maximum attempt count are not claimed. An expired processing lease
already at the maximum is moved directly to `dead` in a bounded data-modifying CTE, so it cannot
remain stuck or cause a sixth provider delivery. Other candidates are ordered by their due
timestamp, creation timestamp, and event ID, then locked with `FOR UPDATE SKIP LOCKED`. This lets
multiple Worker replicas make progress without claiming the same row or waiting for a peer's row
lock.

Each successful claim changes the row to `processing`, increments `attempt_count`, and assigns a
fresh random lease token plus a database-clock expiry. Reclaiming an expired lease is a new delivery
attempt and returns `reclaimed=true`. Candidate selection and all lease updates happen in one
transaction, so a malformed batch or database failure cannot expose partially claimed results.

This remains at-least-once delivery. A Worker crash after Mem0 succeeds but before queue completion
can cause the expired job to be delivered again. The adapter does not claim exactly-once formation.

## Guarded lifecycle transitions

Complete, retry, and dead-letter updates require all of the following in one conditional update:

- the exact event ID;
- `processing` status;
- the current claim's lease token;
- a lease whose expiry is still in the future according to the PostgreSQL clock.

A stale token, terminal row, missing job, or expired lease raises `MemoryJobLeaseLostError` and
does not mutate the row. Completion stores the nonnegative native Mem0 lifecycle-event count.
Retry returns the row to `pending` at an explicit timezone-aware due time and stores only a bounded
error class. Dead-lettering records the sanitized error class and terminal timestamp. No operation
stores exception messages, conversation text, prompts, provider output, or memory facts.

Retry classification, backoff selection, and the decision to dead-letter at the configured final
attempt belong to the Worker orchestration task, not this persistence adapter.

## Administrative reads and mutations

- `stats()` returns only counts by lifecycle state and oldest pending age.
- `list_dead()` is bounded and returns event ID, counters, error class, and timestamps; it omits all
  user, session, conversation, turn, boundary, and content fields.
- `requeue_dead()` acts on one explicit event ID, resets processing attempts, increments the
  requeue counter, and clears terminal/error state.
- `purge_terminal()` deletes one globally bounded, lock-safe batch selected across completed and
  dead retention cutoffs. It never selects pending or processing jobs.

The later operational CLI and retention scheduler will call these port operations; T4.5 does not
add an HTTP mutation endpoint.

## Acceptance evidence

Validated locally on 2026-09-09:

- focused adapter unit tests: 35 passed;
- real PostgreSQL integration and regression suite: 45 passed, 1 skipped;
- full default suite: 453 passed, 47 skipped, 93.08% coverage;
- Ruff lint and format checks passed;
- Alembic autogeneration check reported no pending schema operations;
- Docker image `kira-context:0.4.0-t4.5` built successfully, runs as the non-root `kira` user, and
  imports the packaged PostgreSQL queue adapter;
- real PostgreSQL tests verified due filtering, deterministic claims, `SKIP LOCKED` concurrency,
  retry scheduling, expired-lease reclamation, stale-token rejection, completion, dead listing,
  explicit requeue, statistics, and bounded terminal purge.

The one skipped PostgreSQL-marked test is the existing opt-in semantic provider gate and is not a
T4.5 queue test. Redis remains absent from the runtime and test topology.

## Deferred

T4.5 deliberately does not add the Worker poll loop, `ProcessMemoryUseCase` invocation, retry/error
classification, signal handling, Worker health/metrics endpoints, cleanup scheduling, or admin CLI.
Those are separate Week 4 tasks built on this adapter.
