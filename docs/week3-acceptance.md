# Week 3 — long-term memory acceptance

## Closure verdict

The local implementation scope for T3.1–T3.17 is complete. No further application feature batch
remains after C4. The baseline intentionally stops before automatic online formation, a queue or
worker loop, explicit correction/forget policy, Mem0 REST deployment, LTM authorization, or formal
benchmark tuning.

Two environment gates are still **NOT RUN**: semantic behavior with the approved internal
embedding/memory/rewrite models, and a request against KiRa Test. This workspace has no `.env`, so
there is no endpoint route, model deployment name, or credential to run those gates safely. That is
an external acceptance dependency, not a missing fallback implementation.

## Backlog traceability

| Task | Implemented component/evidence | Status |
| --- | --- | --- |
| T3.1 Pin exact Mem0 OSS | Upstream `v2.0.20`, commit/tree provenance, exact internal dependency `viettel-mem0==2.0.20+viettel.2`, and pristine contract test. | Pass |
| T3.2 Custom package strategy | In-repository fork boundary and patch ledger in `packages/viettel-mem0/UPSTREAM.md` and `PATCHES.md`. | Pass |
| T3.3 Internal package | Local-path package imports under upstream `mem0` namespace with Viettel distribution/version ownership. | Pass |
| T3.4 PostgreSQL + pgvector | pgvector 0.8.6 development image, explicit admin initializer, schema metadata validation, vector dimension and HNSW/user indexes. | Pass |
| T3.5 Embedding integration | OpenAI-compatible embedding configuration, dimension probe, typed failures, and real write/search adapter boundary. Approved provider execution remains an external gate. | Implemented; provider gate NOT RUN |
| T3.6 LongTermMemoryPort | Framework-free `search` and `process_memory` application boundary. | Pass |
| T3.7 Mem0Adapter | Native Mem0 V3 formation and user-scoped retrieval behind typed errors/timeouts; no Mem0 imports in application/domain. | Pass |
| T3.8 User-scoped retrieval | Trusted identity flows into Mem0 filters; adapter rejects mismatched owner payloads; PostgreSQL/pgvector and cross-session isolation tests. | Pass |
| T3.9 Taxonomy v1 | Versioned six-category policy vocabulary, used as extraction guidance rather than persisted metadata. | Pass |
| T3.10 Extraction instructions | Native V3 dual-source policy supports explicit user confirmation of assistant context while blocking inference, authorization, transient results, and secrets. | Pass |
| T3.11 Formula preservation | Exact operator/expression/threshold cases in synthetic scoring, direct formation, and cross-session gates. | Pass |
| T3.12 Negative memory cases | Greeting, ordinary query entity, generated KPI, assistant guess, secret, and prompt-injection cases require zero memory. | Pass locally; live semantic corpus NOT RUN |
| T3.13 Memory search integration | Original current query searches ranked LTM by trusted user and configurable top-k/threshold. | Pass |
| T3.14 ContextBuilder LTM | Ranked LTM is capped and passed separately from bounded Recent and untouched Current. | Pass |
| T3.15 Current > Recent > LTM | Prompt v2 contract plus explicit 90% Current and 85% Recent overrides against remembered 95% in C4. | Pass locally; live rewrite gate NOT RUN |
| T3.16 Cross-session test | Session A persisted boundary forms memory; Session B same-user recall reaches rewrite and KiRa stream; same session ID for another user cannot leak. | Pass with real PostgreSQL/pgvector and deterministic providers |
| T3.17 Search timeout/fallback | Typed timeout/connection/operation/protocol failures preserve Recent + Current, keep `/ready` available, and emit safe logs/metrics. | Pass |

## Architecture invariants at closure

- PostgreSQL conversation messages remain the exact source used by direct memory formation.
- Native Mem0 V3 receives both user and assistant messages and owns lifecycle outcomes. The adapter
  does not coerce results to ADD and the application does not call `update()`/`delete()` in this
  baseline.
- Retrieval uses trusted `user_id`; memory is context only and never grants authorization.
- Current is never trimmed. Explicit Current overrides Recent and LTM; Recent overrides conflicting
  LTM.
- Mem0/embedding/rewrite failures degrade context only. KiRa remains the required answer source and
  no synthetic business answer is generated.
- `LTM_ENABLED=false` is the safe runtime default. Enabling it with incomplete wiring fails startup;
  runtime dependency outages do not by themselves make `/ready` fail.
- Gateway `/chat` does not form memory. `ProcessMemoryUseCase` remains a direct test/dev invocation.
  Durable scheduling/delivery is reserved for the later async-memory phase.

## Local evidence — 2026-09-08

- Upstream provenance trees: both
  `71d41407cefaae26d8cdeb2f180f24bc4b0e90a7`.
- Installed distribution/import version: `2.0.20+viettel.2`; `uv lock --check`: passed.
- Pristine Mem0 contract: 2 passed.
- Ruff lint/format: passed.
- Full application suite: 376 passed, 20 environment-gated tests skipped; coverage 92.49%.
- Full PostgreSQL/pgvector marker: 18 passed, 1 real-provider test skipped.
- Customized Mem0 pgvector suite: 91 passed.
- C4 deterministic cross-session gate: 1 passed, 1 live-provider test skipped.
- Docker image `kira-context:0.3.0`: rebuilt; migration exit 0; Gateway/PostgreSQL healthy;
  non-root runtime UID/GID `10001:10001`.
- Synthetic socket regression: 8/8 cases passed.

## Remaining external acceptance

Run only from an approved network with a disposable migrated database and approved synthetic test
data. Configure the required `MEMORY_*`, `VLLM_*`, KiRa, and PostgreSQL values locally; never commit
the resulting `.env` or raw prompt/response evidence.

```powershell
$env:POSTGRES_TEST_URL="<disposable-postgresql-url>"
$env:RUN_MEMORY_POLICY_EVAL="1"
uv run pytest tests/integration/test_memory_policy_live.py `
  -m memory_llm_integration --no-cov

$env:RUN_CROSS_SESSION_EVAL="1"
uv run pytest tests/integration/postgres/test_cross_session_recall.py `
  -m "postgres_integration and memory_llm_integration" --no-cov
```

After both model gates pass, run the existing Gateway smoke client against KiRa Test with an
approved seed/follow-up pair and retain only sanitized case/outcome/model-deployment evidence.
Until then, Week 3 is code-complete locally but not approved for production model quality.
