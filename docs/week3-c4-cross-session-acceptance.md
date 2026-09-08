# Week 3 Batch C4 — cross-session recall acceptance

## Delivered checkpoint

C4 proves the retrieval path without adding an online memory-formation side effect:

```text
Session A completed turn in PostgreSQL
  -> ProcessMemoryUseCase (direct test/dev invocation)
  -> native Mem0 V3 lifecycle
  -> PostgreSQL/pgvector memory
  -> Session B search scoped by trusted user_id
  -> ContextBuilder (LTM + Recent + Current)
  -> VllmQueryRewriterAdapter
  -> HandleChatUseCase -> KiRa SSE
```

The deterministic gate uses the real conversation adapter, native Mem0 V3 code, customized
pgvector provider, HNSW-backed memory tables, application orchestration, rewrite HTTP adapter, and
Gateway stream lifecycle. Only embedding/memory-model behavior, the rewrite HTTP response, and the
KiRa downstream are deterministic doubles. This separation makes persistence, ownership,
boundaries, prompt inputs, and stream behavior reproducible without claiming real-model quality.

## Acceptance matrix

| Case | Required result |
| --- | --- |
| Session A formation | The exact completed PostgreSQL boundary is sent to Mem0 and one formula memory is persisted. |
| Session B recall | A new session for the same trusted user retrieves the formula and rewrites the follow-up before KiRa. |
| Persistent conversation | The Session B completed turn stores the original user query and exact KiRa answer, not the rewritten query. |
| User isolation | A different user using the same Session B identifier receives neither recent messages nor LTM and KiRa receives the original query. |
| Current precedence | An explicit current threshold of 90% wins over the remembered 95%. |
| Recent precedence | A recent session-local threshold of 85% wins over the remembered 95%. |
| Runtime degradation | A typed memory-search failure bypasses rewriting when no recent context exists, sends the original query to KiRa, and records the safe degraded metric. |
| Data hygiene | Prometheus output contains no user/session/memory identifier or conversation content labels. |

The test also verifies formation metadata contains the exact `conversation_id`, `turn_id`, and
`boundary_message_id`, and that the pgvector collection contains the expected single memory.

## Deterministic local gate

Use a disposable PostgreSQL database with the `vector` extension available. The test creates a
unique memory schema and removes both its conversation rows and schema in `finally` cleanup.

```powershell
$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run pytest tests/integration/postgres/test_cross_session_recall.py --no-cov
```

Expected default outcome is one pass and one skip. The skip is the real-provider semantic gate,
not a skipped deterministic assertion.

## Real-provider semantic gate

The second test is deliberately opt-in. It probes the configured embedding endpoint, initializes a
unique memory schema, invokes the configured memory LLM through native Mem0, searches through the
real embedding endpoint, and invokes the configured vLLM query rewriter. It checks formula
preservation, cross-user isolation, LTM-only recall, and both precedence rules using semantic
assertions rather than one exact generated sentence.

Configure `POSTGRES_TEST_URL`, all required `MEMORY_*` provider settings, `VLLM_BASE_URL`, and
`VLLM_MODEL`, then run:

```powershell
$env:RUN_CROSS_SESSION_EVAL="1"
uv run pytest tests/integration/postgres/test_cross_session_recall.py `
  -m "postgres_integration and memory_llm_integration" --no-cov
```

Setting the opt-in flag without complete endpoint configuration is a failed gate, not evidence of
success. Reports and logs must not capture prompts, memory text, credentials, or raw model output.

## Scope boundary

C4 does not wire `ProcessMemoryUseCase` into `POST /chat`, create a queue/worker loop, or claim a
delivery guarantee. It does not call `Memory.update()` or `Memory.delete()` by application policy.
Formation remains an explicit direct invocation; native Mem0 lifecycle results are accepted without
being coerced to ADD-only. Real KiRa Test and approved internal-model endpoint execution remain
environment gates when credentials/endpoints are unavailable on the developer host.

## Local evidence — 2026-09-08

- Ruff lint and format check: passed.
- Deterministic C4 PostgreSQL/pgvector gate: 1 passed; real-provider semantic gate: 1 skipped by
  explicit opt-in and recorded as **NOT RUN**.
- Full repository suite: 376 passed, 20 environment-gated tests skipped; coverage 92.49%.
- Full PostgreSQL/pgvector marker: 18 passed, 1 real-provider test skipped.
- Customized Mem0 pgvector provider suite: 91 passed.
- Docker image `kira-context:0.3.0` rebuilt; migration exited zero, Gateway/PostgreSQL healthy,
  and runtime UID/GID is the non-root `10001:10001` user.
- Synthetic socket E2E: all 8 Week 2 cases passed with exact persistence and rewrite assertions;
  LTM remained disabled in that regression stack.
- Approved embedding, memory-LLM, rewrite-model, and KiRa Test endpoints: **NOT RUN** on this host.
  Deterministic acceptance is not presented as evidence of production model quality.
