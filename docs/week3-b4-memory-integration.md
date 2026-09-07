# Week 3 Batch B4 — real pgvector formation and policy dev cases

## Delivered checkpoint

B4 exercises the full direct formation path while keeping it outside the online Gateway:

```text
PostgreSQL transactional completed turns
  -> CompletedTurnReference
  -> ProcessMemoryUseCase.read_through_boundary
  -> Mem0Adapter.process_memory
  -> native Mem0 V3 extraction
  -> real PostgreSQL/pgvector write
  -> user-scoped Mem0Adapter.search
```

The integration gate uses the real conversation adapter, real Mem0 V3 engine, real customized
pgvector provider, and a disposable real database schema. Embedding and memory-LLM implementations
are deterministic in-process doubles so the database/adapter contract is stable and requires no
external model credential.

## Acceptance matrix

The selected corpus contains twelve completed two-message conversations.

| Group | Cases | Required persisted result |
| --- | ---: | --- |
| `USER_CONTEXT` | 1 | Explicit responsibility and northern-region scope |
| `ANALYSIS_PREFERENCE` | 1 | Monthly comparison and table preference |
| `USER_DEFINED_METRIC` | 1 | Exact retention formula and `< 95%` threshold |
| `USER_DEFINED_CONVENTION` | 1 | Exact `MTD` convention |
| `TEMPORARY_FOCUS` | 1 | Focus, location, and explicit validity dates |
| `EPISODIC_ANALYSIS_CONTEXT` | 1 | User-confirmed analytical conclusion and date |
| Negative policy cases | 6 | No vector for greeting, query entity, generated KPI result, assistant guess, synthetic secret, or prompt injection |

For every case, the harness:

1. atomically persists user/assistant messages under a unique trusted user and session;
2. calls formation from the exact returned assistant boundary;
3. scores searched memory text with the existing B2 acceptance scorer;
4. searches with a different user and requires zero results;
5. checks boundary metadata on every positive vector.

The formula boundary is processed twice. Native V3 returns an empty second outcome and pgvector
retains one vector, characterizing the current exact-hash, user-scoped dedup behavior. This is not
promoted to durable event idempotency and does not imply automatic UPDATE or DELETE.

The test accepts an empty result as a successful negative/no-change outcome and does not constrain
positive lifecycle actions to a permanent ADD-only port contract. Separate adapter tests continue
to require passthrough for UPDATE, DELETE, NONE, and future well-formed actions.

## Deterministic-provider boundary

- The embedding double returns a stable, non-zero three-dimensional vector. pgvector still owns
  filtering, insertion, cosine search, payload serialization, and indexing.
- The memory-LLM double returns native V3 JSON envelopes selected from the versioned synthetic
  corpus and asserts that `MEMORY_EXTRACTION_INSTRUCTIONS` is present in the actual generated
  prompt.
- Entity extraction is disabled in this gate to avoid testing an unrelated heuristic/entity
  collection path.
- Fixed expected outputs prove persistence and policy wiring, not model intelligence. The approved
  real model must still pass all 18 B2 cases before Week 3 can be accepted externally.

## Scope correction and exclusions

The recovered Week 3 plan specifies that B3/B4 formation is invoked directly. B4 therefore removes
the accidental in-process background runner and `/chat` completion callback introduced in the
preceding implementation commit. The online flow remains exactly Week 2:

```text
recent PostgreSQL -> rewrite -> KiRa SSE -> completed-turn PostgreSQL persistence
```

B4 adds no Redis, queue, worker loop, retry/DLQ, public API, synchronous memory write, LTM context,
or cross-session query rewrite. Those online retrieval concerns begin in Batch C.

## Commands

```powershell
$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run pytest tests/integration/postgres/test_memory_formation.py --no-cov
```

`POSTGRES_TEST_URL` must point to a disposable database with conversation migrations applied and
pgvector available. The test creates a unique memory schema and unique conversation identities,
then removes both in `finally`.

Run the semantic model gate separately when approved endpoints exist:

```powershell
$env:RUN_MEMORY_POLICY_EVAL="1"
uv run pytest tests/integration/test_memory_policy_live.py -m memory_llm_integration --no-cov
```

## Local evidence — 2026-09-07

- Real PostgreSQL/pgvector B4 gate: 1 test, 12 policy cases, passed.
- Full repository suite: 327 passed, 18 environment-gated tests skipped; coverage 91.90%.
- Full PostgreSQL integration marker: 17 passed.
- B1/B2 policy, adapter, and pristine contract gate: 53 passed.
- Internal Mem0 pgvector provider: 91 passed plus 3 subtests.
- Docker image `kira-context:0.3.0` rebuilt as user `kira`; image inspection confirms the direct
  use case is present and the accidental online runner is absent.
- All eight Week 2 socket smoke cases passed and the recreated Gateway container is healthy.
- Six positive vectors persisted; six negative cases persisted none.
- Cross-user search leakage: zero in every case.
- Reprocessing the formula boundary retained exactly one vector.
- Formula and threshold exact-fragment assertions passed.
- Real embedding/memory-LLM semantic gate: **NOT RUN** because approved endpoints are not configured
  on this host.
