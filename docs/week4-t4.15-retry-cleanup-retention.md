# Week 4 T4.15 — Retry, cleanup, and retention

## Outcome

The Worker now owns a second single-use background runner for bounded terminal-job retention.
It starts and stops in the same FastAPI lifespan as the processing runner and queue-metrics sampler.
This task also closes the real-PostgreSQL acceptance gap for the existing five-attempt retry policy.

## Retry contract

`ProcessMemoryJobUseCase` and the PostgreSQL lease queue retain the established policy:

| Failed provider attempt | Persisted transition |
| --- | --- |
| 1 | pending, next attempt after 1 second |
| 2 | pending, next attempt after 5 seconds |
| 3 | pending, next attempt after 30 seconds |
| 4 | pending, next attempt after 120 seconds |
| 5 | dead, with no sixth provider delivery |

Each claim increments `attempt_count`; retry/dead transitions remain protected by the current lease
token. An expired processing lease below the maximum can be reclaimed. An expired lease already at
the maximum is dead-lettered by PostgreSQL without a sixth provider call.

Permanent boundary/configuration/protocol failures still go directly to dead. Cancellation creates
no retry/dead transition. The Worker continues to claim at-least-once and does not claim exactly-once
memory formation.

## Cleanup contract

`MemoryJobCleanupRunner` executes one cleanup operation immediately after Worker startup and then
once per `MEMORY_JOB_CLEANUP_INTERVAL_SECONDS`:

```text
completed_before = current UTC time - MEMORY_JOB_COMPLETED_RETENTION_SECONDS
dead_before      = current UTC time - MEMORY_JOB_DEAD_RETENTION_SECONDS
limit            = MEMORY_JOB_CLEANUP_BATCH_SIZE
```

Defaults are one-hour intervals, seven-day completed retention, thirty-day dead retention, and a
global batch cap of 1,000 rows. The PostgreSQL adapter selects terminal rows deterministically and
uses `FOR UPDATE SKIP LOCKED`; multiple Worker replicas cannot delete the same selected row.

Only `completed` and `dead` rows older than their separate strict cutoffs are eligible. Pending and
processing jobs are excluded by the delete predicate and are never purged, including processing
jobs whose leases have expired. A full batch does not trigger an unbounded drain loop; the next
bounded operation waits for the configured interval.

Each database call uses `MEMORY_JOB_DB_TIMEOUT_SECONDS`. Timeout, connection, operation, protocol,
or unexpected cleanup errors are logged using only the error class and retried at the next cleanup
interval. Cleanup failure does not stop processing or independently change readiness; the runner
and queue-statistics paths retain ownership of readiness. Cancellation propagates and shutdown
interrupts the interval wait immediately.

Successful delete counts feed the existing low-cardinality metrics:

```text
kira_memory_job_cleanup_total{status="completed"}
kira_memory_job_cleanup_total{status="dead"}
```

Metrics and logs contain no event, user, session, conversation, turn, boundary, lease, memory, query,
message, prompt, provider payload, credential, or exception message.

## Acceptance evidence

- Worker cleanup/retry unit gate: 78 passed.
- Full test suite: 567 passed, 62 skipped; total coverage 93.13%.
- Real PostgreSQL integration gate: 60 passed, 1 skipped, including the five-attempt retry schedule
  and terminal-retention test against PostgreSQL.
- Ruff lint and formatting checks passed.
- Docker image `kira-context:0.4.0-t4.15` built successfully. A Worker container started as the
  non-root `kira` user, became Docker-healthy, returned HTTP 200 from `/health` and `/ready`, exposed
  queue database availability `1`, exposed both cleanup counters, and stopped gracefully.

The Docker smoke used an empty queue and intentionally did not call real embedding or memory-LLM
providers. Provider quality and final environment E2E remain outside this task's evidence.

## Scope boundary

T4.15 does not add operator commands, requeue automation, HTTP mutation routes, Compose Worker
wiring, or final release E2E. Explicit `stats`, `list-dead`, and `requeue --event-id` commands remain
T4.16.
