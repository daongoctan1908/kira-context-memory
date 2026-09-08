# Week 3 Batch C3 — runtime wiring and LTM observability

## Delivered checkpoint

C3 activates the C1/C2 retrieval path through the FastAPI lifespan when explicitly configured:

```text
LTM_ENABLED=true
  -> validate Settings
  -> Mem0Adapter.from_settings
  -> HandleChatUseCase(LongTermMemoryPort)
  -> parallel PostgreSQL recent + Mem0 search
  -> prompt v2 -> vLLM rewrite -> KiRa SSE
```

`LTM_ENABLED=false` is the safe default. In that mode no Mem0 client or PostgreSQL memory pool is
created and the online path remains the Week 2 baseline.

## Lifecycle ownership

- A Mem0 adapter created by the app factory is Gateway-owned and closed exactly once at lifespan
  shutdown.
- A port explicitly injected into `create_app` is caller-owned and is never closed by the Gateway.
- Cleanup nesting ensures the KiRa HTTP client, rewrite HTTP client, and SQLAlchemy engine are still
  closed/disposed if owned Mem0 cleanup raises.
- Complete LTM settings are required when the feature flag is enabled. Adapter configuration or
  construction failure fails startup rather than silently claiming the feature is active.
- Runtime search connection/operation/protocol/timeout failures remain soft dependencies and use
  the C2 fallback policy.

The app exposes internal `ltm_status` as `disabled`, `injected`, or `available` for lifecycle tests
and local diagnosis. It is deliberately not a public API or a readiness dependency.

## Readiness semantics

`/ready` continues to describe minimum Gateway readiness. A runtime Mem0 outage does not make it
return 503 because Current + Recent can still reach KiRa. A startup configuration/wiring error while
`LTM_ENABLED=true` prevents the lifespan from becoming ready in the first place.

KiRa remains the only answer source. LTM failure never produces a synthetic answer or memory-only
business response.

## Metrics and safe logging

C3 adds these low-cardinality Prometheus series:

- `kira_memory_search_total{outcome="success|error|bypass"}`;
- `kira_memory_search_duration_seconds{outcome="success|error"}`;
- `kira_memory_search_results` histogram for successful searches, including zero results;
- `kira_context_degraded_total{dependency="mem0",operation="memory_search"}` on typed failure.

Search result count is observed before ContextBuilder's cap, while `MEMORY_SEARCH_TOP_K` is already
bounded to 10. No user ID, session/turn/conversation ID, memory ID, query, prompt, token, credential,
metadata, exception message, or error class is used as a metric label. Sanitized logs contain only
the existing allowlisted correlation/operation/dependency/error-class/fallback fields.

`bypass` means the feature is disabled or trusted identity is unavailable. It has no latency or
result-count observation because no dependency call occurred. Cancellation is not classified as a
dependency error.

## Acceptance coverage

- disabled mode never constructs Mem0;
- enabled mode constructs and closes one owned adapter;
- injected adapters are wired but remain caller-owned;
- online request passes ranked LTM into the rewriter with trusted user scope;
- runtime Mem0 failure preserves SSE and `/ready` 200 while emitting safe error/degraded metrics;
- startup factory failure prevents readiness;
- success, empty, error, timeout, protocol failure, bypass, and unexpected-error metric paths;
- no high-cardinality or sensitive values in Prometheus output;
- all C2 concurrency, cancellation, and fallback tests remain green;
- Week 2 Docker socket behavior remains unchanged when LTM is disabled.

Cross-session semantic recall with real formation, embedding, Mem0 search, and rewrite is C4. C3
does not claim live-model quality from fakes or contract tests.

## Local evidence — 2026-09-08

- Focused lifecycle/online/fallback/telemetry gate: 51 passed.
- Full repository suite: 376 passed, 18 environment-gated tests skipped; coverage 92.49%.
- `HandleChatUseCase`, `ContextTelemetry`, and the observer port are at 100% statement coverage.
- Ruff lint and format check: passed.
- Full PostgreSQL/pgvector integration marker: 17 passed.
- Customized Mem0 pgvector provider gate: 91 passed.
- Docker image `kira-context:0.3.0` rebuilt and runs as non-root user `kira`; migration exited zero,
  Gateway is healthy, and PostgreSQL is healthy.
- Synthetic socket E2E: all 8 Week 2 cases passed. The live metrics endpoint recorded 16 LTM
  bypasses (two requests per case), zero memory dependency calls, and no memory error series.
- Real enabled-LTM/model E2E: **NOT RUN** because approved embedding, memory-LLM, and rewrite-model
  endpoints are not configured on this host. C4 remains the semantic cross-session gate.
