# Week 4 T4.20 — Retry, dead-letter, and requeue E2E

## Outcome

T4.20 verifies the deployed failure-recovery path through the synthetic Week 4 stack:

`Gateway completed turn → PostgreSQL job → Worker retry/dead transition → operator CLI requeue → Worker completion`

The gate uses the real Gateway, PostgreSQL queue adapter, Worker runner, Mem0 adapter, pgvector
store, Worker metrics, and packaged `kira-memory-jobs` CLI. Only the external memory-LLM is a
deterministic fault-injection fixture.

## Fault contract

The memory-LLM mock accepts a bounded `failures` control value and returns a synthetic HTTP 400 for
that many provider calls. HTTP 400 is deliberate: the OpenAI client does not perform a hidden HTTP
retry, while the Mem0 adapter maps the failure to the retryable `LongTermMemoryOperationError` used
by the Worker policy. One observed provider call therefore represents one Worker delivery attempt.

The control endpoint exposes only request, failure, blocked, and remaining-failure counts. It never
returns messages, prompts, memories, user/session/turn IDs, credentials, or provider output.

`compose.week4.yaml` sets retry delays to `0.25/0.25/0.25/0.25` seconds solely to keep this local
five-attempt E2E deterministic and fast. `WorkerSettings` production defaults remain
`1/5/30/120` seconds, and the exact default schedule is already locked by T4.15 PostgreSQL tests.

## Acceptance sequence

### Transient recovery

1. Configure exactly one provider failure.
2. Complete a synthetic chat turn through public `POST /chat`.
3. Require the same durable job to finish `completed` at `attempt_count=2` and
   `requeue_count=0`.
4. Require provider evidence of two calls, exactly one failure, and no remaining failure.
5. Require one lifecycle event, one durable memory, and Worker retry/success metric increments.

### Persistent failure and dead-letter

1. Configure 100 failures, which remains persistent beyond the five-attempt Worker horizon.
2. Complete a second synthetic turn through `POST /chat`.
3. Require the job to become `dead` at exactly `attempt_count=5`, with no lifecycle event or
   durable memory.
4. Require exactly five provider calls/failures and 95 failures still armed. This proves no sixth
   automatic delivery occurred.
5. Execute the packaged CLI `list-dead --limit 1000`, locate the exact event, and require only its
   sanitized projection: event ID, counters, timestamps, and error class.

### Explicit operator recovery

1. Clear the synthetic provider fault to model dependency recovery.
2. Execute packaged CLI `requeue --event-id <exact UUID>` inside the Worker container.
3. Require the CLI result to identify that event and report `requeued=true`.
4. Require the job to finish `completed` with the reset `attempt_count=1`,
   `requeue_count=1`, one lifecycle event, and one durable memory.
5. Require final metric deltas from the whole run: two successes, five retries, and one dead
   transition.

The job is not requeued through SQL or a public HTTP mutation endpoint. Direct SQL is read-only
during assertions and is used for synthetic cleanup only after every gate succeeds.

## Runbook

Recreate the Worker after pulling the Compose setting, then run:

```powershell
docker compose -f compose.week4.yaml up -d --no-build --wait
uv run python -m scripts.smoke_week4_retry
docker compose -f compose.week4.yaml exec -T worker kira-memory-jobs stats
```

`WEEK4_DATABASE_URL` overrides the loopback synthetic database URL. Service URLs, Compose file,
and bounded timeout have CLI options. The runner invokes `docker compose exec -T worker` without a
shell and parses machine-readable JSON from the operator CLI.

Successful runs delete their two synthetic conversations/jobs and matching memories. A failed run
retains evidence for diagnosis; use `docker compose -f compose.week4.yaml down -v` to reset the
disposable stack before retrying if an active failed job could affect fault counters.

## Acceptance evidence

Validated locally on 2026-09-10:

- transient failure: completed at attempt 2 with one lifecycle event and one memory;
- persistent failure: dead at attempt 5 after exactly five provider calls, with no memory;
- packaged CLI listed the exact sanitized dead projection and requeued the exact event;
- recovered job completed at attempt 1 after requeue with `requeue_count=1` and one memory;
- Worker metric deltas were success 2, retry 5, and dead 1;
- post-success cleanup left pending, processing, completed, and dead queue counts at zero;
- provider fault-control and Week 4 smoke-helper tests: 28 passed;
- full default suite: 616 passed, 65 skipped, with 93.12% coverage;
- Ruff lint/format, lock, and Compose configuration checks passed.

## Scope boundary

T4.20 proves bounded Worker retry, dead-letter, and explicit requeue behavior. Crash-after-write
lease recovery is covered separately by T4.21. Queue-database outage readiness, production provider
semantics, and final release evidence remain T4.22–T4.23; exactly-once formation is out of scope.
