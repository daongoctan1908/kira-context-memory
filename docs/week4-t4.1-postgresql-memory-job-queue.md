# Week 4 T4.1 PostgreSQL memory job queue decision

## Status

Accepted on 2026-09-08 for the Week 4 asynchronous memory baseline.

## Context

The Gateway already persists complete user and assistant turns in PostgreSQL and returns an exact
`CompletedTurnReference` containing the trusted user, conversation, turn, and assistant boundary.
`ProcessMemoryUseCase` can read the bounded snapshot ending at that boundary and invoke native
Mem0 V3 without placing extraction on the online read path.

The earlier master design proposed Redis for both recent conversation data and Redis Stream memory
events. The project subsequently made PostgreSQL the conversation source of truth and removed the
unused Redis conversation implementation. Week 4 therefore needs a new queue decision that does
not reintroduce a second stateful dependency without evidence.

A direct PostgreSQL-then-Redis dual write would leave a failure window: the completed turn could
commit while the stream publish is lost. Adding a PostgreSQL outbox, relay, Redis Stream, consumer
group, and Redis dead-letter stream would close that window but introduce two queue layers and a
new workload before the baseline has demonstrated that PostgreSQL claiming is a bottleneck.

## Decision

PostgreSQL `memory_jobs` is the only asynchronous memory queue in the Week 4 baseline.

- A completed turn and its optional memory job are inserted in one PostgreSQL transaction.
- Gateway scheduling is controlled by `MEMORY_FORMATION_ENABLED`, independently from online
  retrieval through `LTM_ENABLED`.
- A separate Worker process claims due rows with `FOR UPDATE SKIP LOCKED`, then invokes
  `ProcessMemoryUseCase` using the exact persisted boundary.
- The queue contains references and operational state only. It does not copy conversation text,
  prompts, model responses, credentials, or memory content.
- Delivery is at-least-once. A lease protects active work and allows another Worker to reclaim an
  abandoned job.
- One initial attempt plus at most four retries are allowed. Retry delays are 1, 5, 30, and 120
  seconds; the fifth failed provider attempt moves the job to `dead`.
- Completed jobs are retained for 7 days and dead jobs for 30 days by configurable cleanup.
- Operators may inspect sanitized queue statistics/dead rows and requeue one explicit `event_id`
  through a local administrative CLI. No mutation HTTP API is added.
- The Worker is a separate command/process in the existing application image and exposes internal
  `/health`, `/ready`, and `/metrics` endpoints.

Redis, Redis Stream, an outbox relay, and a stream consumer group are not part of this baseline.
No Redis dependency, configuration, container, key, stream, or test fixture should be restored by
the Week 4 implementation.

## Transaction and idempotency boundary

The PostgreSQL transaction is the durable boundary for scheduling:

```text
insert completed user and assistant messages
  -> insert memory_jobs reference
  -> commit both or roll back both
```

No extraction LLM, embedding request, Mem0 formation, or pgvector memory write runs inside this
transaction. Those operations happen only after a Worker has claimed the committed job.

The queue uses a stable random `event_id` and a unique assistant `boundary_message_id`. Duplicate
turn persistence must return the existing job reference instead of creating another job. Claim,
complete, retry, and dead-letter transitions must use a per-claim lease token so a stale Worker
cannot overwrite a newer owner.

Exactly-once memory formation is not claimed. If Mem0 succeeds and the Worker crashes before it can
record completion, the job may be processed again after its lease expires. Native Mem0 duplicate
behavior and the stable persisted boundary reduce duplicate formation, but they are not a
cross-system transaction guarantee.

## Runtime and failure behavior

- When memory formation is disabled, completed turns continue to persist without queue rows.
- A partial/failed KiRa stream, empty assistant response, or detected client disconnect creates
  neither a completed turn nor a job, preserving the existing completion contract.
- Failure to insert the job rolls back the completed-turn transaction. Because KiRa text may have
  already been streamed, Gateway records a persistence error but does not emit a synthetic answer.
- PostgreSQL queue outage stops new Worker claims and makes Worker readiness fail until polling
  recovers. It does not change Gateway's existing availability policy.
- Retryable dependency failures are rescheduled with bounded backoff. Invalid boundaries,
  configuration/schema mismatches, and protocol-invalid job data go directly to `dead`.
- Cancellation or graceful shutdown leaves an in-flight job under its lease; expiry enables safe
  reclamation without manufacturing a failure result.
- Per-job provider failures are observable through retry/dead metrics but do not crash the Worker
  process.

## Scaling and operational consequences

Multiple Worker replicas may claim jobs concurrently because locked rows are skipped. Scaling must
be driven by pending count, oldest due age, processing latency, retry rate, and database contention,
not CPU alone.

This decision removes Redis operational cost and the PostgreSQL-to-Redis consistency gap, but adds
queue writes, claim scans, leases, terminal-row retention, and cleanup load to PostgreSQL. Partial
indexes for due pending jobs and expired leases are mandatory. Claim batch size and concurrency
must remain bounded so memory extraction cannot exhaust PostgreSQL or model connections.

Completed and dead rows are automatically purged after their configured retention windows. Pending
and processing jobs are never deleted by cleanup. Requeue must happen before a dead row reaches its
retention deadline.

## Alternatives not selected

**Direct Redis Stream publish after PostgreSQL commit.** Rejected because a process crash can leave
a durable conversation turn without a corresponding memory event.

**PostgreSQL transactional outbox plus Redis Stream.** Rejected for the Week 4 baseline because the
relay, stream, consumer group, Redis persistence, and dual monitoring planes add complexity without
current throughput evidence.

**Redis as conversation source or recent cache.** Rejected because PostgreSQL is the established
source of truth and recent reads have not been shown to be a bottleneck.

**External broker or Mem0 REST service.** Deferred until a platform standard, independent release
boundary, or measured scale requirement exists.

## Revisit triggers

Reconsider this decision only when measured evidence shows one or more of the following:

- queue claim/update traffic materially degrades conversation-write or recent-read latency;
- PostgreSQL locking, vacuum, cleanup, or connection pressure prevents the required Worker
  throughput;
- pending age cannot meet the memory freshness objective with bounded Worker concurrency;
- multiple regions or applications require broker-native fan-out and independent retention;
- the infrastructure platform mandates a supported enterprise broker;
- queue RPO/RTO cannot be met by the PostgreSQL deployment.

If a broker is introduced later, preserve the application-facing job port and exact PostgreSQL
boundary. Use a transactional outbox/relay migration; do not replace atomic scheduling with a
best-effort direct publish.

## T4.1 acceptance criteria

- The decision explicitly supersedes Redis Stream portions of the original baseline for Week 4.
- PostgreSQL remains the conversation source of truth and future Worker input source.
- Atomic scheduling, at-least-once limitations, lease/retry/dead behavior, retention, scaling, and
  revisit triggers are unambiguous.
- Redis remains absent from runtime dependencies and Compose definitions.
- No migration, queue implementation, Worker loop, or Gateway wiring is introduced in T4.1.
