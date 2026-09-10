# Week 4 T4.21 — Crash and lease recovery

## Outcome

T4.21 proves the strongest deployed at-least-once failure boundary in the Week 4 baseline:

`Mem0 durable write → queue completion blocked → Worker SIGKILL → lease expiry → new Worker reclaim`

The test does not merely kill a Worker before it performs useful work. It first observes the
synthetic memory in pgvector, then confirms the original Worker is waiting on the PostgreSQL
`memory_jobs` completion update, and only then terminates the process with `SIGKILL`.

## Synthetic lease override

`compose.week4.crash.yaml` is a test-only override used together with the base stack. It reduces the
Worker conversation timeout to 1 second, memory operation timeout to 2 seconds, and lease to 5
seconds. The lease remains greater than the combined operation deadlines, so the same
`WorkerSettings` validation applies.

The base Compose stack and production defaults remain unchanged at a 120-second lease and a
30-second memory-operation timeout. The crash runner always recreates the base Worker in `finally`,
even when an assertion fails.

## Deterministic sequence

1. Recreate only the Worker with the crash override and require `/ready` to return 200.
2. Reset the synthetic memory-LLM in blocked mode and complete one Gateway chat turn.
3. Require one provider request blocked and the corresponding job `processing` at attempt 1.
4. Start a separate PostgreSQL transaction and take a row lock on that exact `event_id`.
5. Release memory-LLM and require exactly one matching pgvector memory.
6. Query `pg_stat_activity` for one lock-waiting `UPDATE memory_jobs`; no query text is printed.
7. Send `SIGKILL` to the Worker while its completion transition is blocked.
8. Roll back the test lock and require the durable job to remain `processing` at attempt 1.
9. Start a new Worker with the same crash override and wait for the five-second lease to expire.
10. Require a reclaimed claim, attempt 2 completion, exactly two provider calls, and still exactly
    one durable memory.
11. Delete the synthetic conversation/job and memory, then recreate the base Worker.

On attempt 2, native Mem0 V3 finds the identical user-scoped hash and emits zero new lifecycle
events. Zero events is a successful processing result, so the Worker completes the job with
`lifecycle_event_count=0`.

## Runbook

Start the base stack first, then run the self-contained crash acceptance:

```powershell
docker compose -f compose.week4.yaml up -d --no-build --wait
uv run python -m scripts.smoke_week4_crash
docker compose -f compose.week4.yaml exec -T worker kira-memory-jobs stats
```

The runner accepts explicit base/override Compose paths, service URLs, database URL, and bounded
timeout. Docker Compose is invoked directly without an intermediate shell. Provider output,
database URLs, message content, and subprocess stderr are never printed on failure.

## Acceptance evidence

Validated locally on 2026-09-10:

- the first Worker was lock-blocked after one durable memory write and before queue completion;
- `SIGKILL` left the job `processing` at attempt 1 with no terminal fields;
- a new Worker reclaimed the expired lease and completed the job at attempt 2;
- the second native Mem0 call emitted zero lifecycle events and exact-hash dedup kept one memory;
- restarted-Worker metrics reported at least one reclaimed claim and one success;
- provider evidence reported exactly two calls and no injected provider failure;
- post-success cleanup left every queue status at zero;
- base Worker was restored with lease 120 seconds and memory operation timeout 30 seconds;
- Week 4 smoke/provider focused tests: 32 passed;
- full default suite: 620 passed, 65 skipped, with 93.12% coverage;
- Ruff lint/format and merged Compose configuration checks passed;
- T4.19 and T4.20 deployed E2E gates passed again after base Worker restoration.

## Scope boundary

This is evidence for at-least-once lease recovery of an identical deterministic fact. It is not a
general exactly-once guarantee and does not add application-level deduplication beyond native Mem0
V3 exact-hash behavior. PostgreSQL outage/readiness is covered separately by T4.22; final release
evidence belongs to T4.23.
