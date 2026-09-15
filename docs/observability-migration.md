# Observability migration plan

Status: Phases 0–2 complete. Durable Gateway-to-Worker context begins in Phase 3. Every later
phase has an independent acceptance gate and may be rolled back without reverting business data.

The architecture and identifier rules are normative in
[`observability-architecture.md`](observability-architecture.md). Data capture and retention are
normative in [`observability-data-policy.md`](observability-data-policy.md).

## Migration decisions

| Current component | Decision | When | Reason and risk control |
| --- | --- | --- | --- |
| `ContextObserverPort` boundary | Keep and extend as a framework-free application boundary | Phase 2 | It keeps OTel/Langfuse SDK types out of domain/application code. All calls must become fail-open. |
| Gateway/Worker `prometheus-client` registries | Migrate to OTel Meter, then remove | Dual-read in Phase 5; remove in Phase 7 | One instrumentation API avoids duplicate counters and backend coupling. Parity tests protect names, labels, buckets, and outcomes. |
| Gateway/Worker `/metrics` | Keep during parity, remove after cutover | Phase 7 | Prometheus will ingest OTel metrics from the Collector; retaining both paths permanently would double operational work. |
| Queue snapshot sampler | Keep | All phases | It supplies Worker readiness as well as gauge data. Only its metric sink changes. |
| Python logging API | Keep | All phases | Existing code already emits operational events. Formatter/configuration are unified and enriched with context. |
| Current allowlist JSON formatter | Replace | Phase 1 | It omits essential timestamp/severity/event and trace fields. The replacement retains content-free defaults. |
| Raw HTTPX/Mem0 logs | Continue suppressing | All phases | They can contain URLs, payloads, prompts, or credentials. Purpose-built spans expose safe outcomes. |
| Typed errors, retry, fallback, leases, receipts | Keep unchanged | All phases | These are business correctness controls, not observability implementation. |
| Langfuse trace storage | Add | Phase 6 | It provides AI-oriented observations and filtering. It is not the metrics/log backend. |
| Tempo/Jaeger | Do not add to pilot | Phase 0 decision | Langfuse is the pilot trace store; a second trace backend would duplicate storage and operations. |

## Phase dependencies

```text
Phase 0 contract and baseline
    -> Phase 1 OTel runtime and safe logging
        -> Phase 2 Gateway/SSE trace
            -> Phase 3 durable job context and Worker trace
                -> Phase 4 AI-stage observations
                    -> Phase 5 metric parity
                        -> Phase 6 Kubernetes/backends/retention
                            -> Phase 7 chaos, soak, cutover, cleanup
```

Phase 5 can build metric adapters after Phase 1, but its parity gate waits for the stage/outcome
contract from Phases 2–4. Phase 6 can prepare infrastructure earlier, but production-like ingest
acceptance waits for Phases 1–5.

## Phase 0 — contract and baseline

Status: **DONE**.

Created:

- `docs/observability-architecture.md` — current audit, signal ownership, four-ID contract, span
  catalog, async context carrier, metrics map, log schema, outage semantics, version matrix, and
  baseline evidence;
- `docs/observability-migration.md` — phased work, files, tests, acceptance, dependencies, rollout,
  rollback, and removal gates; and
- `docs/observability-data-policy.md` — content classification, masking, cardinality, sampling,
  access, retention, and failure behavior.

Acceptance evidence:

- baseline checkpoint fixed at `9e007ee`;
- 730 unit/contract/vendor tests passed;
- repeatable in-process Gateway and Worker mock timings recorded;
- Week 4 Compose/PostgreSQL happy-path smoke passed across Gateway, Worker, asynchronous memory
  formation, durable memory retrieval, rewrite, and KiRa SSE; the first response-before-formation
  observation was 1.340 seconds and is recorded as smoke evidence, not a latency distribution;
- architecture decisions no longer leave correlation/trace identity, signal ownership, async
  trace shape, initial sampling, or retention for an implementer to choose.

## Phase 1 — shared OTel runtime and safe logging

Status: **DONE**.

Goal: establish an optional, bounded, fail-open telemetry runtime before instrumenting business
stages.

Modify:

- `pyproject.toml`, `uv.lock`;
- `app/config/settings.py`, `worker/settings.py`;
- `app/presentation/api/main.py`, `worker/main.py`;
- `app/infrastructure/observability/context.py`, `worker/telemetry.py`.

Create:

- `app/infrastructure/observability/runtime.py`;
- `app/infrastructure/observability/settings.py`;
- `app/infrastructure/observability/tracing.py`;
- `app/infrastructure/observability/logging.py`;
- `app/infrastructure/observability/redaction.py`;
- `app/infrastructure/observability/langfuse_attributes.py`;
- `compose.observability.yaml` and `deploy/observability/collector-local.yaml`.

Key changes:

- one `TracerProvider` and one `MeterProvider` per process, initialized and shut down once;
- OTLP/HTTP batch export to the Collector with bounded queues/timeouts;
- no-op implementations when disabled or initialization fails in a recoverable environment;
- one structured logger configuration for Gateway and Worker;
- context injection for independent correlation, trace/span, turn, and event IDs;
- content capture disabled at generic HTTP/DB instrumentation boundaries.

Tests:

- repeated init/shutdown and dependency-injected application lifespans;
- unavailable/slow Collector, queue saturation, export rejection, and flush deadline;
- redaction failure and context isolation between concurrent tasks;
- tracing disabled while correlation logging remains active.

Acceptance:

- Gateway and Worker behavior is unchanged with telemetry disabled or Collector absent;
- no duplicate providers, handlers, log records, or exporters;
- observer failures cannot escape into application or Worker code;
- pinned dependencies and local Collector config match the Phase 0 version matrix.

Acceptance evidence:

- OTel API, SDK, OTLP/HTTP exporter, and semantic-convention transitive dependency are locked to
  the reviewed `1.44.0` / `0.65b0` line in `uv.lock`;
- the Collector `0.160.0` image is pinned by tag and digest, and its local configuration passes the
  Collector `validate` command;
- unit/contract/vendor suite passes with `758 passed`, and coverage remains above the repository
  gate at `90.26%`;
- the overlay stack is healthy and accepts an explicit OTLP probe (`1` span and `1` metric point);
- the full asynchronous Compose smoke passes both with Collector available and while the Collector
  container is stopped; Gateway and Worker readiness remain HTTP 200 during the outage; and
- Prometheus business metrics remain unchanged in this phase. Business spans and OTel metric
  instruments intentionally begin in Phases 2 and 5.

Dependency: Phase 0.

## Phase 2 — Gateway and SSE trace

Status: **DONE**.

Goal: trace one `/chat` through response streaming and clean-completion persistence while keeping
application correlation independent.

Modify:

- `app/presentation/api/chat_router.py`, `sse.py`, and `errors.py`;
- `app/application/use_cases/handle_chat.py`;
- `app/domain/ports/context_observer.py`;
- `app/infrastructure/kira/http_kira_client.py` and `token_manager.py`.

Create:

- `app/presentation/api/correlation_middleware.py`;
- `app/presentation/api/tracing_middleware.py`.

Key changes follow the Gateway span catalog in the architecture contract. Correlation middleware
runs outside request validation; tracing middleware reads but never creates/replaces the
application correlation ID. The root span stays active through SSE close and persistence.

Tests:

- validation 422, pre-stream 502/504, mid-stream error, successful stream, disconnect, and
  persistence fallback;
- recent read and memory search remain concurrent and share the request context;
- tracing disabled, inbound `traceparent`, and concurrent requests all retain distinct
  application correlation IDs;
- public SSE frames, error JSON, and `X-Correlation-ID` remain compatible.

Acceptance:

- one root span covers the actual streamed request lifetime;
- first-event, first-content, stream, and persistence timings are present;
- `correlation_id != trace_id` is enforced by construction and tests;
- no partial answer is persisted after cancellation.

Acceptance evidence:

- pure ASGI correlation runs before request validation, always creates an application-owned ID,
  and preserves the existing `X-Correlation-ID`, JSON error, and SSE frame contracts;
- `chat.request` remains active through upstream SSE close and the clean-completion persistence
  callback; application stage spans share that root even when recent reads and memory search run
  concurrently;
- KiRa instrumentation records token cache outcome, stream open/outcome, first event, first
  content, and bounded provider request/message IDs without recording prompts, answers, tokens, or
  credentials;
- acceptance tests cover 422, 502, 504, mid-stream failure, successful persistence, persistence
  fallback, disconnect/cancellation, ignored public `traceparent`, and concurrent correlation
  isolation;
- the complete suite passes with `787 passed`, `70 skipped`, and `92.76%` coverage; and
- the rebuilt Compose stack passes the asynchronous Gateway/Worker/PostgreSQL smoke while the
  Collector receives a 20-span batch, and structured Gateway close logs contain independent
  `trace_id`, `correlation_id`, and `turn_id` values; the same smoke also passes while the Collector
  is stopped, after which Collector health recovers normally.

Dependency: Phase 1.

## Phase 3 — durable context and Worker attempts

Goal: connect chat, job, and every Worker delivery across process restart and retry.

Modify:

- `app/infrastructure/postgres/schema.py`, `conversation_store.py`, `managed_store.py`, and
  `memory_job_queue.py`;
- `app/domain/ports/conversation_store.py` and `app/domain/models/memory_job.py`;
- `app/application/use_cases/handle_chat.py` and `process_memory_job.py`;
- `worker/runner.py`, `runtime.py`, and `job_admin.py`.

Create:

- `app/domain/models/telemetry_context.py`;
- an additive Alembic migration after `20260908_0003`;
- `tests/integration/postgres/test_memory_job_trace_context.py`.

Key changes:

- nullable, versioned 1 KiB `telemetry_context` carrier;
- independent correlation and optional W3C context stored in the existing enqueue transaction;
- one linked Worker root trace per attempt;
- queue age, attempt/requeue number, claim/reclaim, process, and transition observation;
- instrumentation for the SQL path that marks exhausted reclaimed jobs dead.

Database rollout:

1. Release a bridge build that accepts only the explicitly listed old and new revisions but does
   not use the new column.
2. Wait until every Gateway, Worker, and admin process runs the bridge build.
3. Apply the nullable additive migration.
4. Roll out carrier-aware code.
5. Roll back application code only to the bridge build if needed; retain the additive column.

Tests:

- enqueue rollback and duplicate scheduling;
- old `NULL` jobs, malformed/oversized carrier, and invalid trace context with valid correlation;
- retry, reclaim, manual requeue, lease loss, attempt exhaustion, and process restart;
- mixed revisions during the defined rollout sequence.

Acceptance:

- correlation finds the source chat, event, and every attempt;
- each attempt has a new trace with a link to the producer when available;
- retry/lease/idempotency decisions and provider call counts match the control behavior;
- telemetry context can never make a valid job unprocessable.

Dependency: Phase 2.

## Phase 4 — AI-stage observations

Goal: identify whether observed bad output originated in retrieval, context construction, rewrite,
KiRa, or a Mem0 formation substage.

Modify:

- `app/application/services/context_builder.py`;
- `app/infrastructure/llm/vllm_query_rewriter.py`;
- `app/infrastructure/memory/mem0_adapter.py`;
- `app/application/use_cases/process_memory.py`;
- `packages/viettel-mem0/mem0/memory/main.py`;
- `packages/viettel-mem0/mem0/llms/vllm.py`;
- `packages/viettel-mem0/mem0/embeddings/openai.py`.

Create:

- `app/infrastructure/observability/memory_observer.py`;
- `packages/viettel-mem0/mem0/observability.py`.

Key changes:

- optional no-op observer hook inside vendored Mem0 with no OTel/Langfuse dependency;
- retrieval, extraction, parse, embedding, dedup, receipt, and persistence events;
- provider usage captured before adapters reduce responses to strings/vectors;
- invocation-local context preserved through `asyncio.to_thread`;
- masked and bounded AI input/output attached using Langfuse OTel attributes.

Tests:

- malformed extraction, valid empty result, dedup-to-empty, provider failure, receipt replay;
- absent/malformed usage and safe truncation;
- observer failure and concurrent thread context;
- exact comparison of provider requests, results, memory rows, and receipts with observer on/off.

Acceptance:

- each failure fixture points to one distinguishable stage/outcome;
- observation changes do not alter prompts, calls, facts, lifecycle events, or receipts;
- raw Mem0/HTTPX logging remains suppressed.

Dependency: Phase 3.

## Phase 5 — OTel metric parity

Goal: replace direct metric instrumentation without losing operational coverage.

Modify:

- `app/infrastructure/observability/context.py`, `worker/telemetry.py`;
- `worker/runtime.py`, `worker/runner.py`;
- existing Gateway and Worker telemetry tests.

Create:

- `app/infrastructure/observability/metrics.py`;
- `deploy/observability/prometheus.yaml`;
- `deploy/observability/grafana/` provisioning, dashboards, and alerts.

Key changes:

- implement the metric mapping frozen in the architecture document;
- add stage and end-to-end measurements;
- observable gauges read only the cached queue/runtime snapshot;
- keep readiness updates outside telemetry callbacks;
- compare old scrape and new OTel paths in the migration environment.

Tests:

- metric count, outcome, bucket, unit, and restart parity;
- multi-Worker queue aggregation uses `max`, not `sum`;
- bounded attribute-set tests reject all request/user/job identifiers;
- metrics export failure does not change readiness.

Acceptance:

- every retained metric has a reviewed OTel equivalent;
- dashboards and alerts use only the new backend data and do not double-count;
- all intentional semantic/name changes are recorded.

Dependency: Phases 1–4.

## Phase 6 — Kubernetes, backends, and retention

Goal: deploy the internal pilot using versioned, private infrastructure.

Create:

- `deploy/kubernetes/kira/` Helm chart for Gateway, Worker, services, probes, and migration job;
- `deploy/kubernetes/observability/` values/config for Collector, Langfuse, Prometheus, Loki,
  Grafana, NetworkPolicies, secrets, and retention CronJob;
- `scripts/purge_langfuse_traces.py` and unit tests;
- `docs/observability-runbook.md`.

Key changes:

- Collector agent DaemonSet for stdout logs and one pilot Collector gateway with persistent queue;
- applications know only the Collector endpoint; Langfuse ingestion credentials live in the
  Collector Secret;
- separate Langfuse PostgreSQL, ClickHouse, Valkey, and object storage from the business database;
- internal TLS/ingress, RBAC, NetworkPolicy, requests/limits, PVCs, and secret references;
- hourly bounded Langfuse OSS deletion job for the 14-day cutoff;
- Loki 14-day and Prometheus 30-day retention.

Tests:

- Helm render/schema and disposable-cluster deployment;
- trace, metric, and log ingest/query plus cross-links;
- NetworkPolicy, pod restart, unavailable backend, secret scan;
- retention cutoff, pagination, retry, idempotent rerun, and deletion verification.

Acceptance:

- no backend or credential is publicly exposed or committed;
- observability outage does not change application/Worker probes;
- expired telemetry is deleted and cleanup backlog is observable;
- exact chart/image versions and digests are locked.

Dependency: Phases 1–5 plus deployment-specific StorageClass, ingress hostname, and Secret values.

## Phase 7 — chaos, soak, cutover, and cleanup

Goal: prove safety and remove superseded observability paths.

Modify/remove only after the acceptance gate:

- remove Gateway `app/presentation/api/metrics_router.py` registration and Worker `/metrics`;
- remove `prometheus-client` and process-local registries;
- update `scripts/smoke_week4_observability.py`, its tests, and `README.md`.

Create:

- `tests/integration/test_observability_pipeline.py`;
- `scripts/smoke_observability.py`.

Required scenarios:

- Collector unavailable/slow, Langfuse 401/429/5xx/timeout, exporter queue full;
- metrics and log backends failing independently;
- Worker crash before/after formation commit and lease reclaim without duplicate facts;
- SSE disconnect and concurrent requests during telemetry failure;
- disabled/sampled-out tracing while correlation survives Gateway to Worker;
- secrets/PII in input, output, provider error, and exception paths;
- cleanup outage, Collector restart, and backend disk pressure.

Final acceptance:

- business outputs, database writes, retry decisions, and provider calls match telemetry-off
  controls;
- p95 overhead on the frozen local mocks is at most `max(10 ms, 5% of baseline)` for Gateway and
  Worker measurements;
- CPU/memory remain bounded during backend outage;
- OTel metrics pass parity and the pilot completes a 48-hour soak;
- correlation finds chat, job, and attempts while remaining independent of every trace ID;
- no forbidden value appears in privacy fixtures;
- one metric instrumentation path and one trace/log export path remain.

Dependency: Phases 1–6.

## Rollback boundary

The checkpoint `9e007ee` is the pre-observability code reference. Runtime phases should be committed
separately so each can be reverted without crossing business changes. Schema Phase 3 uses an
additive nullable column and a bridge release; application rollback does not require dropping it.
Telemetry infrastructure can be disabled through configuration before uninstalling any backend.

No rollback procedure may use telemetry loss as a reason to reset business PostgreSQL, delete
memory rows, or bypass a queue lease/receipt invariant.
