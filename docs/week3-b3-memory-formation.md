# Week 3 Batch B3 — completed-turn memory formation

## Delivered boundary

Batch B3 connects the already-persisted chat completion to native Mem0 V3 formation:

```text
KiRa normal SSE EOF
  -> transactional PostgreSQL append of original user + exact assistant text
  -> newly-inserted CompletedTurnReference
  -> managed in-process background task
  -> PostgreSQL read_through_boundary
  -> MemorySource validation
  -> LongTermMemoryPort.process_memory
  -> Mem0 add(..., user_id=trusted_user_id, infer=true)
```

This is write-side formation only. It does not search memories, add LTM to the query-rewriter
context, or claim cross-session recall; those remain Batch C.

## Source and isolation guarantees

- Formation receives only `CompletedTurnReference` produced by the transactional PostgreSQL
  adapter. Client-supplied `user_id`, `turn_id`, conversation ID, or message boundary are never
  accepted.
- `ProcessMemoryUseCase` re-reads the source of truth with all of
  `user_id + conversation_id + boundary_message_id`. PostgreSQL verifies that the boundary is an
  assistant message owned by that user and returns old-to-new messages ending exactly there.
- `MEMORY_FORMATION_MESSAGE_LIMIT` defaults to 10 and must be even and at least two. The domain
  rejects empty, odd, cross-session, wrong-boundary, or non-paired user/assistant snapshots before
  Mem0 is called.
- Both user and assistant messages are preserved. This follows native Mem0 V3 source handling and
  allows explicit user confirmations to adopt assistant-proposed details under policy v2.
- Mem0 is always called with the trusted reference `user_id` and boundary metadata. Redis is not a
  source, and no request-local or partial stream text is passed directly to formation.

## Lifecycle and idempotency semantics

- The adapter calls only native `add(..., infer=true)`. It has no application path to explicit
  `update()` or `delete()` in this batch.
- Every well-formed provider lifecycle action is returned unchanged and in order. The current
  pristine v2.0.20 V3 engine is characterized as ADD-or-no-change, but the application does not
  encode that as a permanent protocol restriction.
- Formation is submitted only when `AppendTurnResult.inserted` is true. A duplicate transactional
  append does not submit the same boundary again.
- This suppression is not a durable exactly-once guarantee. There is no outbox, worker, Redis
  Stream, PostgreSQL job queue, or broker in B3. A process crash between commit and task completion
  can lose formation work. Durable delivery belongs to the later async-memory phase.

## Runtime and degradation behavior

- `LTM_ENABLED=false` constructs no Mem0 adapter and no formation runner.
- With LTM enabled, Mem0 wiring/configuration errors fail startup. Runtime PostgreSQL source-read,
  Mem0 timeout/connection/protocol, and unexpected formation errors are consumed at the background
  boundary and do not generate a synthetic answer or `gateway_error` SSE frame.
- The response path schedules work after conversation commit and does not await Mem0. On shutdown,
  the app drains current tasks up to `MEMORY_OPERATION_TIMEOUT_SECONDS`, then cancels what remains
  before closing its owned Mem0 client.
- Safe logs contain only correlation ID, operation, dependency, error class, and fallback mode.
  They never include conversation content, extracted facts, prompts, tokens, credentials, user ID,
  session ID, turn ID, or memory ID.

Prometheus adds bounded-cardinality metrics:

- `kira_memory_formation_total{outcome=processed|no_change|error|cancelled}`;
- `kira_memory_formation_duration_seconds{outcome=...}`;
- `kira_memory_formation_events`;
- existing `kira_context_degraded_total` operations `memory_dispatch`, `memory_source_read`, and
  `memory_formation`.

Provider lifecycle action strings are deliberately not metric labels because providers may add
new actions and turn them into unbounded cardinality.

## Acceptance coverage

- Exact user/conversation/boundary call and bounded chronological pair source.
- Empty, cross-session, odd, role-reversed, and wrong-turn boundaries fail before Mem0.
- Native lifecycle results, including unknown future well-formed actions, survive unchanged.
- New completed turn submits exactly once; duplicate, partial/error/textless stream, failed append,
  and missing identity do not submit.
- PostgreSQL and Mem0 failures are classified and consumed without changing SSE.
- Background completion, no-change, unexpected error, and shutdown cancellation are observable
  without high-cardinality or sensitive labels.
- LTM-disabled wiring is inert; an app-owned Mem0 adapter is closed after runner shutdown.

## Environment-gated evidence

The deterministic suite uses a fake `LongTermMemoryPort`; adapter contract tests use a fake Mem0
client. PostgreSQL exact-boundary and pgvector schema/provider suites remain separate real-database
gates. A real memory-model formation run is not claimed unless the approved internal embedding and
memory LLM endpoints are configured and the B2 18-case semantic gate passes first.

## Local checkpoint evidence — 2026-09-07

- Ruff lint and format checks pass.
- Full repository suite: 340 passed, 17 environment-gated tests skipped; coverage 92.21%.
- PostgreSQL/pgvector integration suite against the isolated synthetic database: 16 passed.
- Internal Mem0 pgvector provider suite: 91 passed plus 3 subtests.
- Docker image `kira-context:0.3.0` rebuilt successfully, runs as user `kira`, and replaced the
  local Week 2 Gateway container.
- Week 2 socket regression remains green for all eight location/time/metric/reference/comparison,
  standalone, topic-switch, and injection cases.
- Real memory formation and the B2 semantic gate: **NOT RUN** because this host has no configured
  memory LLM or embedding endpoint/model. No production-readiness claim is made from fake-model
  tests.
