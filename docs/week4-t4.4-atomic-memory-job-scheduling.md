# Week 4 T4.4 atomic memory job scheduling

## Delivered checkpoint

T4.4 makes PostgreSQL persistence the single commit boundary for a completed conversation turn and
its optional asynchronous memory job:

```text
completed KiRa stream
  -> insert user + assistant messages
  -> insert reference-only pending memory job when enabled
  -> commit both, or roll back both
```

The existing SSE contract is unchanged. Memory extraction, embedding, Mem0, job claiming, and
Worker execution never run in the Gateway request or completion callback.

## Application contract

`ConversationStorePort.append_turn()` now accepts the keyword-only `schedule_memory` flag.
`AppendTurnResult` returns the exact completed-turn reference plus an optional stable
`memory_job_event_id`. The core still has no SQLAlchemy or PostgreSQL imports.

`MEMORY_FORMATION_ENABLED` defaults to false and is independent from `LTM_ENABLED`:

- formation disabled: persist the completed turn without a queue row;
- formation enabled: persist the completed turn and queue row in one transaction;
- LTM retrieval may remain disabled while scheduling is enabled;
- missing identity, failed/partial stream, explicit disconnect, or empty assistant text persists
  neither a turn nor a job.

A post-stream PostgreSQL failure remains a degraded persistence outcome. The Gateway records its
existing sanitized log/metric and does not append a synthetic SSE answer.

## PostgreSQL idempotency

The adapter schedules by the trusted assistant `boundary_message_id`. Insert uses the unique
boundary constraint with conflict-safe lookup:

- the first schedule creates a random stable `event_id` and pending schema-v1 row;
- a duplicate completed-turn write validates the exact persisted pair and returns the existing
  event ID;
- repeated scheduling never creates a second job for the same boundary;
- failure to insert the job aborts the transaction, including a newly inserted conversation and
  both messages.

Only the boundary and operational job fields are written. Conversation text is not copied to the
queue.

## Deferred

T4.4 does not implement `MemoryJobQueuePort`, `FOR UPDATE SKIP LOCKED`, leases, retries,
dead-letter transitions, cleanup, Worker processes, or administrative requeue. Those begin in
T4.5 and later tasks.

## Local acceptance evidence

Validated on 2026-09-08:

- focused unit/API contract suite: 149 passed;
- real PostgreSQL scheduling/schema/conversation suite: 37 passed;
- full suite: 418 passed, 42 skipped, 93.11% coverage;
- Ruff lint and format checks passed;
- rebuilt Docker Gateway with formation temporarily enabled: HTTP 200 produced exactly one pending
  job referencing the assistant message; migration exited 0 and Gateway stayed healthy;
- the synthetic smoke conversation/job was deleted afterward through its exact session ID, and the
  local Gateway was restored to the safe default `MEMORY_FORMATION_ENABLED=false` with `/ready`
  still passing.
