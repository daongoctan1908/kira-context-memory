# Memory formation idempotency

## Correctness key

`memory_jobs.event_id` identifies one complete formation attempt across claim, lease reclaim,
memory persistence, and queue completion. `turn_id`, exact memory text, semantic similarity, and
retrieval rank are not idempotency keys.

The application carries the UUID through `ProcessMemoryJobUseCase`, `ProcessMemoryUseCase`,
`MemorySource`, and `Mem0Adapter` as `metadata.formation_event_id`.

## State transitions

| Queue state | Receipt state | Worker behavior |
| --- | --- | --- |
| `processing` | absent | run native V3 formation and atomically commit all memories + receipt |
| `processing` after reclaim | present | return receipt directly, skip providers, complete queue |
| `processing` after reclaim | absent | previous formation did not commit; run formation again |
| `completed` | present | terminal normal state; no further claim |

The receipt may contain an empty result, which is still a committed formation.

## Failure boundaries

- Crash before memory transaction commit: PostgreSQL rollback leaves no memory and no receipt;
  reclaim performs formation.
- Crash after memory transaction commit but before queue complete: reclaim sees the receipt and
  skips formation.
- Concurrent execution of one event: the receipt primary key elects one transaction; other
  contenders return the winner's committed result.
- Vector batch failure: receipt and every row roll back together; per-row fallback is disabled for
  event-scoped formation.
- Different event IDs that generate semantically equivalent facts remain native Mem0 policy, not
  queue idempotency.

## Acceptance coverage

- unit: `event_id` propagation and exact receipt preflight before search/embedding/LLM;
- custom Mem0: conflict returns first result even when the losing output is a paraphrase;
- real pgvector: one memory for competing paraphrases and transaction rollback on mid-batch error;
- schema: fresh version 2 init plus controlled version 1 upgrade without vector loss;
- deployed Compose: SIGKILL before and after formation commit with lease reclaim.
