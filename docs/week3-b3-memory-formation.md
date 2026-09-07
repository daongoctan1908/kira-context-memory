# Week 3 Batch B3 — exact-boundary memory formation use case

## Delivered boundary

Batch B3 adds the direct application use case prepared for dev/integration harnesses and a future
worker:

```text
CompletedTurnReference
  -> PostgreSQL read_through_boundary
  -> MemorySource validation
  -> LongTermMemoryPort.process_memory
  -> ordered MemoryProcessResult
```

It deliberately does not add a public endpoint, queue, worker loop, or `/chat` completion callback.
Online retrieval and Gateway-owned Mem0 lifecycle wiring remain Batch C.

## Source and isolation guarantees

- The input is a `CompletedTurnReference` produced by transactional PostgreSQL persistence.
- `ProcessMemoryUseCase` reads with the complete
  `user_id + conversation_id + boundary_message_id` key and a configurable even message cap.
- `MemorySource` rejects an empty or odd snapshot, cross-session data, incomplete/reversed
  user/assistant pairs, mismatched turn IDs, and a final message that is not the referenced
  assistant boundary.
- Both user and assistant messages remain available to native Mem0 V3. This preserves the policy-v2
  confirmation/reference behavior rather than imposing a user-message-only extractor.
- Store and Mem0 typed errors propagate unchanged to the caller. A future worker owns retry and
  failure policy; the use case does not silently report success.

## Lifecycle semantics

- The adapter still calls only `add(..., infer=true)` and exposes no application path to explicit
  `update()` or `delete()`.
- Every well-formed lifecycle action is returned unchanged and in provider order. The pristine
  v2.0.20 V3 engine is separately characterized as ADD-or-empty; that behavior is not forced into
  the port contract.
- Duplicate formation is not suppressed by the use case. B4 verifies the pristine engine's exact
  hash/user-scope behavior against pgvector; durable event idempotency remains future work.

## Acceptance coverage

- Exact read arguments and bounded old-to-new source.
- Full source validation before Mem0 is called.
- Preservation of ADD, UPDATE, DELETE, NONE, and future well-formed actions returned by the port.
- Propagation of PostgreSQL and Mem0 typed failures.
- No Redis, queue, worker, HTTP API, or KiRa SSE callback.

## Local checkpoint evidence — 2026-09-07

- Ruff lint and format checks passed.
- Full repository suite passed with coverage above 90%.
- PostgreSQL exact-boundary integration, pgvector provider tests, Docker build, and all eight Week 2
  SSE smoke cases remained green.
- Real memory-model formation was not claimed without approved internal endpoints.
