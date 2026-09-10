# Week 4 T4.21 — Event-idempotent crash and lease recovery

## Outcome

T4.21 now verifies memory formation idempotency by `memory_jobs.event_id`, rather than treating
Mem0 V3 exact-text hash dedup over semantic `top_k=10` as a correctness boundary.

The deployed flow is:

`claim event_id → exact receipt lookup → V3 formation → atomic memories + receipt commit → queue complete`

If a reclaimed job already has a committed receipt, formation returns that receipt before message
history, embedding, semantic search, or LLM extraction. The Worker then performs only the separate,
idempotent queue-completion transition.

## Transaction review

The queue uses SQLAlchemy/asyncpg while custom Mem0 owns a separate synchronous psycopg pool. They
cannot safely share one local PostgreSQL transaction. They also do not need to: at-least-once queue
delivery intentionally commits after the durable memory boundary.

The atomic boundary is therefore inside the memory database:

- `memory.memories_formation_receipts.event_id` is the primary key;
- each memory payload persists the same `formation_event_id` provenance;
- one psycopg transaction inserts the receipt and every vector memory;
- any vector failure rolls back both receipt and all rows;
- an `ON CONFLICT (event_id)` loser reads and returns the first committed result without inserting;
- an empty V3 extraction still commits an empty receipt, so it is not re-extracted on retry.

SQLite Mem0 history and entity linking remain derived, best-effort operations after this commit.
They are deliberately outside the correctness boundary; long-term facts and their receipt are the
authoritative formation output.

## Schema contract

Memory schema version 2 adds:

```sql
CREATE TABLE memory.memories_formation_receipts (
    event_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    result JSONB NOT NULL,
    memory_count INTEGER NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The actual DDL also checks non-blank users, JSON-array results, and `memory_count` consistency. A
partial expression index on `memories.payload->>'formation_event_id'` supports provenance audits.
The admin initializer performs a controlled upgrade only from schema version 1 paired with
`viettel-mem0==2.0.20+viettel.2`; unknown version tuples fail closed.

## Crash acceptance phases

`scripts.smoke_week4_crash` executes the phases sequentially with a five-second synthetic lease:

1. **Crash before formation commit:** block the memory LLM, observe attempt 1 in `processing`, kill
   the Worker, and require zero memories plus zero receipts. After lease reclaim, attempt 2 calls
   the provider and commits exactly one memory/receipt. Total provider calls: two.
2. **Crash after formation commit:** block queue completion with a row lock, release the LLM, wait
   for exactly one memory and receipt, then kill the Worker. Attempt 2 reclaims the job, reads the
   receipt, and completes without a second provider call. Total provider calls: one.

The second phase is independent of output wording and top-k membership: no second extraction or
semantic retrieval occurs. Focused tests additionally force semantic search to fail on replay and
offer a paraphrased second formation result; the committed receipt still wins.

## Runbook

```powershell
docker compose -f compose.week4.yaml up -d --no-build --wait
uv run python -m scripts.smoke_week4_crash
docker compose -f compose.week4.yaml exec -T worker kira-memory-jobs stats
```

The runner restores the base Worker in `finally`. Provider payloads, database URLs, credentials,
and conversation content are not printed.

## Acceptance evidence

Validated locally on 2026-09-11 with image `kira-context:0.4.1`:

- crash before commit: zero memory and zero receipt at `SIGKILL`; reclaimed attempt 2 completed
  with one memory/receipt and two total provider calls;
- crash after commit: one memory/receipt existed before `SIGKILL`; reclaimed attempt 2 completed
  with the original lifecycle result and one total provider call;
- both recovered jobs completed at attempt 2 and the base 120-second Worker was restored;
- post-smoke synthetic queue, memory, and receipt counts returned to zero.

## Scope boundary

This guarantees idempotent memory formation per `event_id` while queue delivery remains
at-least-once. It is not a distributed transaction between queue and memory databases, does not
deduplicate different event IDs, and does not make derived SQLite history/entity links atomic.
