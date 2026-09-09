# Week 4 T4.14 — Worker FastAPI app

## Outcome

The asynchronous memory runner now has a dedicated internal HTTP process:

```text
python -m worker.main
```

The process owns the validated T4.11 dependency lifespan, the T4.13 runner, and a cached queue
statistics sampler. It exposes only three read-only routes; OpenAPI and interactive documentation
are disabled and there is no HTTP mutation, dead-letter, requeue, or cleanup API.

## Lifecycle and probes

- `/health` returns 200 whenever the process event loop can serve the request. It does not probe
  PostgreSQL, Mem0, embedding, or memory-LLM providers.
- `/ready` returns 200 only while the runner task is active, its claim polling has observed the
  queue database as available, and the independent queue-statistics snapshot is successful and
  fresh.
- `/ready` returns 503 before lifespan startup, during shutdown, after a queue polling/statistics
  failure, when the cached snapshot becomes stale, or if the runner exits unexpectedly.
- A statistics snapshot is fresh for `2 × MEMORY_JOB_METRICS_REFRESH_SECONDS +
  MEMORY_JOB_DB_TIMEOUT_SECONDS`. This allows one scheduled refresh interval before declaring a
  silent sampler stale.
- Probe and metrics requests never query PostgreSQL. Queue statistics refresh in a bounded
  background task using the configured DB timeout.
- Configuration or schema mismatch still fails startup in the dependency lifespan. Runtime queue
  loss keeps liveness available but fails readiness until both polling paths recover.

Application shutdown first stops further statistics refresh and asks the runner to stop claiming.
The runner then applies the T4.13 grace-period drain. A job cancelled after the grace period gets no
synthetic transition and remains recoverable through lease expiry.

## Prometheus contract

The Worker uses a process-local registry separate from the Gateway registry:

| Metric | Labels |
| --- | --- |
| `kira_memory_job_queue_depth` | `status=pending|processing|completed|dead` |
| `kira_memory_job_oldest_pending_age_seconds` | none |
| `kira_memory_job_claim_total` | `kind=new|reclaimed` |
| `kira_memory_job_processing_total` | `outcome=success|retry|dead` |
| `kira_memory_job_processing_duration_seconds` | bounded `outcome` |
| `kira_memory_job_attempt_count` | none |
| `kira_memory_job_lifecycle_event_count` | none |
| `kira_memory_job_cleanup_total` | `status=completed|dead`; populated by T4.15 |
| `kira_memory_worker_runner_active` | none |
| `kira_memory_job_queue_database_available` | none |
| `kira_memory_job_in_flight` | none |
| `kira_memory_job_database_backoff_seconds` | none |

Claims are observed only after PostgreSQL returns valid leases. Processing outcomes are observed
only after the corresponding complete/retry/dead transition succeeds. A transition failure emits
no false outcome and leaves the job for lease reclaim. Telemetry failure is isolated from job
processing.

Metric labels and safe Worker logs never contain event, user, session, conversation, turn,
boundary, memory or lease identifiers; query/message/prompt/provider content; credentials;
exception messages; or error class labels.

## Container contract

The shared image still starts the Gateway by default. Run the Worker by overriding its command:

```text
python -m worker.main
```

The image exposes ports 8000 and 8001 and runs as non-root user `kira`. Its health check reads
`HEALTH_PORT`, defaulting to Gateway port 8000. A Worker container therefore sets
`HEALTH_PORT=8001`; `WORKER_PORT` remains the actual Worker bind-port setting. Compose wiring stays
out of scope until T4.18.

## Scope boundary

T4.14 does not add retention cleanup scheduling, operator CLI commands, Docker Compose Worker
service, release orchestration, or provider-quality claims. Those remain T4.15–T4.23.

## Acceptance evidence

Validated locally on 2026-09-09:

- Worker-focused unit gate: 65 passed, covering probes, route surface, cached metrics, readiness
  failure/recovery/staleness, runner exit, DB timeout, telemetry isolation, safe labels, entrypoint,
  and lifecycle shutdown;
- full default suite: 554 passed, 60 skipped, 93.08% coverage;
- full PostgreSQL/pgvector marker: 58 passed, 1 existing semantic provider gate skipped;
- Ruff lint and format checks passed;
- image `kira-context:0.4.0-t4.14` built with version label `0.4.0-t4.14`, non-root user
  `kira`, and exposed ports 8000/8001;
- a real container ran `python -m worker.main` against the local PostgreSQL queue, became Docker
  `healthy`, returned HTTP 200 from `/health` and `/ready`, and exposed fresh zero-depth queue
  gauges plus active-runner/database-available gauges from `/metrics`;
- the smoke container accepted a normal stop and was removed after graceful lifespan shutdown.

Provider endpoints were not called because the acceptance queue was empty. This verifies the
Worker runtime/control plane, not memory-model quality; internal provider gates remain separate.
