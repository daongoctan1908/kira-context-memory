# Week 4 T4.2 memory job core contract

## Delivered checkpoint

T4.2 defines the framework-free contract that the PostgreSQL queue adapter and Worker will
implement in later tasks. It does not create a database table, change completed-turn persistence,
or start background work.

`MemoryJob` represents one actively leased job and contains:

- a stable UUID `event_id`;
- the exact `CompletedTurnReference` required by `ProcessMemoryUseCase`;
- a positive attempt count;
- a UUID lease token and timezone-aware expiry;
- whether the claim reclaimed an abandoned lease;
- schema version 1.

The completed-turn reference is excluded from the model representation to reduce accidental
identity/session disclosure in logs. Job models contain no conversation content, prompts,
credentials, model responses, or memory facts.

## Queue port

`MemoryJobQueuePort` owns these capability boundaries:

- claim due or abandoned work with a lease owner, bounded limit, lease duration, and max attempts;
- complete, retry, or dead-letter only with the current event/lease token;
- return aggregate sanitized statistics;
- list a bounded dead-job projection and requeue one explicit event;
- purge bounded expired completed/dead jobs without touching pending/processing work.

The port returns immutable tuples and typed domain models. Transition methods return no ambiguous
boolean; an adapter must raise `MemoryJobLeaseLostError` when a conditional lease transition no
longer owns the row. `requeue_dead` is the only boolean operation because a missing/non-dead event
is a normal administrative outcome.

## Error contract

The queue exposes sanitized configuration, connection, operation, protocol, and lost-lease errors.
Messages never include a DSN, SQL statement, event identifier, user identifier, conversation
content, or provider output. Lost lease is a specialized operation error so a Worker may handle it
without treating stale ownership as a database outage.

Dead-job projections permit only a bounded Python-style error class name, attempt/requeue counts,
event ID, and timestamps. They deliberately omit user, session, turn, conversation, and boundary
identifiers.

## Validation invariants

- IDs and lease tokens are real UUID values, not arbitrary strings.
- Claimed attempts start at one.
- All operational timestamps are timezone-aware.
- Counts are nonnegative integers and reject booleans.
- Queue age is finite and nonnegative when present.
- Dead time cannot precede creation time.
- Only schema version 1 is accepted.
- Queue status is restricted to pending, processing, completed, or dead.

## Deferred to later tasks

- T4.3 adds the migration and SQLAlchemy schema.
- T4.4 adds atomic completed-turn and job scheduling.
- T4.5 implements this port using PostgreSQL row locking and leases.
- Worker retry classification, poll loops, health/readiness, metrics, cleanup scheduling, and CLI
  orchestration remain Batch C.

No Redis type, key, stream, client, or dependency is present in this contract.
