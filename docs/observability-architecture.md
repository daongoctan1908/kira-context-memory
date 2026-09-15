# Unified observability architecture

Status: Phase 0 contract and Phase 1 shared runtime complete on 2026-09-15. Business-stage spans,
OTel metric migration, Langfuse, Loki, Grafana, and Kubernetes deployment remain later phases.

## Goals

The observability path must answer four questions without changing the chat or memory behavior:

1. Which application request, conversation turn, and distributed trace produced an outcome?
2. Which memory job and Worker attempts originated from a completed chat turn?
3. How long did each Gateway, provider, persistence, queue, and formation stage take?
4. When an AI result is wrong, was the observed cause retrieval, context construction, rewrite,
   KiRa, or memory formation?

The application must continue serving chat and processing memory when any telemetry component is
slow, unavailable, misconfigured after startup, or rejecting data. Telemetry may be dropped after
bounded buffering; it must never control business retry, fallback, lease, idempotency, readiness,
or persistence decisions.

## Current-state audit

This table records the pre-integration baseline at checkpoint `9e007ee`; Phase 1 closes the shared
runtime and logging gaps described below, while stage instrumentation remains intentionally open.

| Area | Existing support | Gap to close |
| --- | --- | --- |
| Gateway correlation | `/chat` creates an application UUID, returns `X-Correlation-ID`, and uses it in sanitized errors. | Creation occurs inside the route and does not cover every ASGI error path. The ID stops at the Gateway. |
| Gateway metrics | A process-local Prometheus registry records context size, search/rewrite outcomes and durations, degradation, writes, and job scheduling. | There is no request trace and no timing for identity, recent reads, context construction, KiRa streaming, or persistence. |
| Worker metrics | A process-local registry records queue state, claims, attempts, processing, cleanup, runner state, and database backoff. | There is no trace per job attempt. Some transition errors and cancellations have no processing duration. |
| Logging | Gateway and Worker emit allowlisted JSON fields and suppress verbose HTTPX/Mem0 logs. | The formatter omits timestamp, severity, event name, logger, trace ID, span ID, turn ID, and event ID. Gateway and Worker configuration is duplicated. |
| Error handling | Typed KiRa, rewriter, conversation, memory, and queue errors drive sanitized responses, fallbacks, retries, and dead-letter behavior. | Errors identify a class and aggregate outcome but cannot be located within a complete request/job timeline. |
| PostgreSQL job boundary | A completed turn and optional `memory_jobs` row commit in one transaction; `event_id` remains stable across retries. | The job row does not carry application correlation or W3C trace context. |
| Mem0 | Formation is idempotent by `formation_event_id`; the vendored pipeline has distinct retrieval, extraction, parse, embedding, dedup, and persist steps. | Only the outer `mem0.add()` duration is visible. A malformed extraction response can currently become an empty fact result, which is indistinguishable in outer telemetry. |
| KiRa SSE | The Gateway retains KiRa request/message IDs and safely proxies frames. | There is no first-event, first-content, stream, or close timing and no trace propagation policy. |
| Deployment | Docker and Compose support deterministic development stacks. | The repository has no Kubernetes or telemetry-backend deployment definitions. |

The following behavior is independent of observability and remains authoritative:

- typed errors and the public KiRa error/SSE contracts;
- context degradation and original-query fallbacks;
- KiRa token caching, invalidation, and one authentication retry;
- atomic completed-turn persistence and memory-job scheduling;
- queue claim ordering, `SKIP LOCKED`, lease tokens, retry, reclaim, requeue, dead-letter, and
  cleanup;
- formation receipts and at-least-once idempotency by `event_id`;
- cancellation behavior that avoids persisting incomplete streamed answers; and
- the Worker queue sampler used by readiness. Moving its metric output must not remove its
  readiness state.

## Signal ownership

| Signal | Instrumentation and transport | Backend | Primary use |
| --- | --- | --- | --- |
| Traces | OTel API/SDK in Gateway and Worker, exported by OTLP/HTTP through the Collector | Langfuse self-hosted | Request/job timelines and AI input/output investigation |
| Metrics | OTel Meter API, exported through the Collector | Prometheus | SLOs, queue health, saturation, errors, and alerts |
| Logs | Python structured JSON to stdout, collected from Kubernetes container logs | Loki | Operational events and investigation when traces are absent |
| Dashboards | Prometheus and Loki data sources with links to Langfuse | Grafana | Operational overview |

Langfuse is the only trace store in the pilot. Tempo and Jaeger are not deployed. Application
processes send OTLP only to the in-cluster Collector. They do not export the same signal directly
to Langfuse and the Collector at the same time.

Python logging remains the application logging API. It is not a second telemetry export path.
The Collector reads container stdout once, enriches it with Kubernetes resource metadata, and
sends it to Loki.

## Identifier contract

The following identifiers are independent and must never be derived from or substituted for one
another:

| Identifier | Owner | Scope |
| --- | --- | --- |
| `correlation_id` | Gateway application | One incoming chat request and any memory job originating from it |
| `trace_id` | OpenTelemetry | One distributed trace; the Gateway trace and each Worker attempt trace may differ |
| `turn_id` | Conversation application logic | One user/assistant conversation turn |
| `event_id` | Memory-job application logic | One durable memory job across claim, retry, reclaim, requeue, and completion |

`correlation_id` is generated at the Gateway request boundary and continues to work when tracing
is disabled, sampled out, or unavailable. It remains in `X-Correlation-ID` and public error
payloads. An inbound `traceparent` cannot select or replace it.

Trace and log records carry all identifiers available at that point. A typical lineage is:

```text
Gateway          trace_id=AAA correlation_id=BBB turn_id=CCC event_id=DDD
Worker attempt 1 trace_id=EEE correlation_id=BBB turn_id=CCC event_id=DDD
Worker attempt 2 trace_id=FFF correlation_id=BBB turn_id=CCC event_id=DDD
```

The Worker attempt traces link to the Gateway producer span in trace `AAA`. The stable
`correlation_id`, `turn_id`, and `event_id` also support investigation when one of the traces was
not recorded. These values are trace/log fields and are never metric labels.

Raw `user_id` and `session_id` are not telemetry identifiers. Langfuse user and session fields use
the HMAC pseudonyms defined in the data policy.

## Gateway trace contract

`chat.request` is a server root span created at the ASGI boundary. Its lifetime includes the whole
SSE response and the clean-completion persistence callback; returning a `StreamingResponse` does
not end it.

| Span name | Kind / Langfuse type | Required outcome or measurements |
| --- | --- | --- |
| `chat.request` | SERVER / chain | `success`, `degraded`, `error`, or `cancelled`; HTTP status; total duration |
| `identity.resolve` | INTERNAL / span | authenticated, anonymous, or error outcome |
| `conversation.read_recent` | CLIENT / span | success/error, returned count, duration |
| `memory.search` | CLIENT / retriever | success/error/bypass, configured top-k/threshold, returned and selected counts |
| `context.build` | INTERNAL / chain | success/error, recent and memory counts, estimated tokens, trim reason |
| `rewrite.generate` | CLIENT / generation | success/error/bypass, model, known token usage, fallback mode |
| `kira.authenticate` | CLIENT / span | success/error, cache miss or refresh; never token content |
| `kira.chat` | CLIENT / generation | open/stream outcome, first-event and first-content times, KiRa bounded IDs |
| `conversation.append_turn` | CLIENT / span | inserted/duplicate/error, duration |
| `memory_job.enqueue` | PRODUCER / span | scheduled/disabled/duplicate/error and `event_id` after commit |

`kira.chat` does not create a span per SSE chunk. First event means the first valid KiRa data
event; first content means the first non-empty text fragment. Neither is claimed to be the model's
native time-to-first-token.

Recent-message loading and long-term-memory search remain concurrent child spans. Their durations
must not be added to calculate request latency. An SSE error after HTTP headers were sent marks the
trace error even though the HTTP response status is already 200. Client disconnect is recorded as
`cancelled`, not as a KiRa failure.

Inbound W3C trace context is disabled by default for public clients. A later deployment may enable
it only behind a trusted internal proxy. Baggage is never trusted or propagated. Outbound trace
headers are sent only to explicitly allowlisted internal hosts.

## Durable asynchronous boundary

The `memory_jobs` table receives one nullable `telemetry_context JSONB` column in Phase 3. The
version 1 carrier has this logical shape:

```json
{
  "version": 1,
  "correlation_id": "32 lowercase hexadecimal application ID",
  "traceparent": "optional W3C trace parent",
  "tracestate": "optional validated W3C trace state"
}
```

The serialized value is limited to 1 KiB and contains no baggage, prompt, message, user/session
identity, credentials, or arbitrary metadata. `correlation_id` and W3C fields are validated
independently: invalid trace context does not discard a valid application correlation ID. Invalid
or oversized telemetry data must not fail enqueue, claim, or processing.

The enqueue producer span is active while the completed turn and job are committed. Each Worker
delivery creates a new root consumer span, with a Span Link to the stored producer context. Retry,
lease reclaim, and manual requeue create new attempt traces while retaining `correlation_id`,
`turn_id`, and `event_id`. A legacy job with no carrier still runs and receives a new Worker trace;
the Worker does not invent a source correlation ID.

| Worker span | Kind / Langfuse type | Required details |
| --- | --- | --- |
| `memory_job.process` | CONSUMER / chain | event, attempt/requeue number, reclaimed flag, queue age, outcome |
| `conversation.read_boundary` | CLIENT / span | boundary ID, message count, duration, safe error class |
| `mem0.formation` | INTERNAL / chain | receipt hit/miss and aggregate formation outcome |
| `mem0.existing_memory.search` | INTERNAL / retriever | count and duration |
| `mem0.extract` | CLIENT / generation | masked prompt/output, model settings, known usage, provider outcome |
| `mem0.extract.parse` | INTERNAL / span | parsed, empty-valid, or malformed outcome; fact count |
| `mem0.memory.embed` | CLIENT / embedding | batch/item count, model, duration; no vectors |
| `mem0.deduplicate` | INTERNAL / span | candidate, duplicate, and retained counts |
| `mem0.persist` | CLIENT / span | receipt conflict/created and stored count |
| `memory_job.transition` | CLIENT / span | complete/retry/dead/lease-lost/error |

Empty extraction, parse failure, full deduplication, an idempotent receipt replay, and a provider
failure are distinct outcomes. Observability hooks report the existing behavior; changing Mem0
parsing or lifecycle behavior requires a separate business change.

## Metric contract and migration map

OTel instrument names use dots and canonical units. The Prometheus backend may translate dots to
underscores and append conventional suffixes. Dashboards bind to the exported names verified in
Phase 5, not to an assumed translation.

| Existing Prometheus metric | OTel instrument | Action |
| --- | --- | --- |
| `kira_context_recent_messages` | `kira.context.recent_messages` | Migrate histogram |
| `kira_context_estimated_recent_tokens` | `kira.context.estimated_recent_tokens` | Migrate histogram; retain “estimated” meaning |
| `kira_memory_search_total` | `kira.memory.search.count` | Migrate counter |
| `kira_memory_search_duration_seconds` | `kira.memory.search.duration` (`s`) | Migrate histogram |
| `kira_memory_search_results` | `kira.memory.search.result_count` | Migrate histogram |
| `kira_memory_job_schedule_total` | `kira.memory.job.schedule.count` | Migrate counter |
| `kira_context_rewrite_total` | `kira.context.rewrite.count` | Migrate counter |
| `kira_context_rewrite_duration_seconds` | `kira.context.rewrite.duration` (`s`) | Migrate histogram |
| `kira_context_degraded_total` | `kira.context.degraded.count` | Migrate counter |
| `kira_conversation_write_total` | `kira.conversation.write.count` | Migrate counter |
| `kira_memory_job_queue_depth` | `kira.memory.job.queue.depth` | Migrate observable gauge from cached snapshot |
| `kira_memory_job_oldest_pending_age_seconds` | `kira.memory.job.oldest_pending.age` (`s`) | Migrate observable gauge |
| `kira_memory_job_claim_total` | `kira.memory.job.claim.count` | Migrate counter |
| `kira_memory_job_processing_total` | `kira.memory.job.process.count` | Migrate counter; map old `success` to `completed` |
| `kira_memory_job_processing_duration_seconds` | `kira.memory.job.process.duration` (`s`) | Migrate histogram |
| `kira_memory_job_attempt_count` | `kira.memory.job.attempt.number` | Migrate histogram |
| `kira_memory_job_lifecycle_event_count` | `kira.memory.lifecycle_event.count` | Migrate histogram |
| `kira_memory_job_cleanup_total` | `kira.memory.job.cleanup.count` | Migrate counter |
| `kira_memory_worker_runner_active` | `kira.memory.worker.runner.active` | Migrate observable gauge |
| `kira_memory_job_queue_database_available` | `kira.memory.job.queue.database.available` | Migrate observable gauge |
| `kira_memory_job_in_flight` | `kira.memory.job.in_flight` | Migrate observable gauge |
| `kira_memory_job_database_backoff_seconds` | `kira.memory.job.database_backoff` (`s`) | Migrate observable gauge |

Phase 5 also adds request duration, first-event/first-content duration, recent-read duration,
context-build duration, KiRa stream duration, persistence duration, queue wait, formation-stage
duration, and telemetry dropped/export-failure signals.

Metric attributes are limited to reviewed bounded enums: `service.name`, environment, stage,
operation, outcome, dependency, queue status, claim kind, and cleanup status. No correlation,
trace, span, user, session, conversation, turn, boundary, provider-request, memory, or event ID is a
metric attribute.

Queue depth describes one shared PostgreSQL queue. Every Worker replica currently observes the
same global counts, so dashboards aggregate this gauge with `max`, not `sum`.

## Logging contract

Every application JSON log contains `timestamp`, `severity`, `event`, `service.name`, and
`deployment.environment`. It adds `trace_id` and `span_id` when a recording context exists, and
adds `correlation_id`, `turn_id`, and `event_id` when known. Optional bounded fields include
`operation`, `dependency`, `outcome`, `error_class`, `fallback_mode`, and `attempt_count`.

Logs do not serialize message/prompt/response content, exception text, stack traces from untrusted
provider data, HTTP headers, URLs containing query strings, or credentials. AI content belongs in
masked Langfuse observations. Repeated exporter failures are rate-limited to avoid recursive log
storms.

## Error and outage semantics

Instrumentation is called through a fail-open facade. Observer failures can emit a bounded local
diagnostic but cannot replace a return value, raise into business code, suppress cancellation, or
alter an error class.

The SDK uses bounded batch queues and never awaits network export in a request or job. Collector
export pipelines use independent queues, memory limits, retries, and bounded persistence so a
Langfuse outage does not block Prometheus or Loki. Health and readiness do not depend on the
Collector or any telemetry backend. Shutdown attempts a bounded flush only after business cleanup.

Loss of telemetry after queue exhaustion is an accepted pilot failure mode and must be measurable.
It is preferable to delayed chat responses, expired leases, or duplicated memory formation.

## Phase 1 runtime boundary

Gateway and Worker each own one `ObservabilityRuntime` for their FastAPI process lifespan. The
runtime creates a private `TracerProvider` and `MeterProvider` rather than replacing process-global
providers; later manual instrumentation receives its tracer/meter from this runtime. This keeps
dependency-injected tests isolated and prevents repeated global-provider registration. Generic
HTTP/DB auto-instrumentation and content capture are not enabled.

`OTEL_ENABLED=false` is a true no-op. When enabled, the application exports traces and metrics via
OTLP/HTTP only to the configured Collector base URL, using bounded trace queues and exporter
timeouts. Missing endpoint, construction failure, export rejection, queue saturation, or bounded
shutdown expiry cannot escape into business code. Correlation context remains available whether
tracing is disabled, unsampled, or unavailable.

The local Compose overlay starts the pinned Collector with OTLP gRPC/HTTP receivers, a memory
limiter, bounded batches, debug trace output, and a Prometheus endpoint for received OTel metrics.
It is a development transport test, not the Phase 6 Langfuse/Loki/Grafana deployment.

## Version compatibility baseline

Versions below are the Phase 0 reviewed pins, not floating “latest” references. Phase 1 and Phase 6
must lock them in `uv.lock`, image digests, and Helm lock files and run compatibility tests before
promotion.

| Component | Phase 0 pin | Reason/constraint |
| --- | --- | --- |
| Application | `0.4.1`, Python `3.11.9` | Current runtime baseline |
| `viettel-mem0` | `2.0.20+viettel.3` | Current vendored formation contract |
| OTel Python API/SDK | `1.44.0` | API, SDK, and OTLP HTTP exporter stay on the same stable line |
| OTel semantic conventions, if imported directly | `0.65b0` | Must match the `1.44.0` Python release line; avoid direct dependency unless needed |
| OTel Collector Contrib | `0.160.0` | Required for OTLP, filelog, Kubernetes enrichment, filtering, and OTLP/HTTP export |
| Langfuse Helm chart | `2.1.0` | Use the chart's tested dependency set |
| Langfuse application | `4.24.0` | Chart `2.1.0` default; do not override it with a newer app until chart tests pass |
| Prometheus | `3.14.0` | OTLP receiver backend for OTel metrics |
| Loki | `3.7.7` | OTLP-compatible log backend through the Collector |
| Grafana | `13.2.1` | Dashboard/UI baseline |

The upstream Langfuse application release was newer than the chart default when this matrix was
recorded. Compatibility with the pinned chart takes priority over independently selecting the
newest application image.

## Phase 0 evidence

Baseline commit: `9e007ee1f9c0097d1a8e7c47abbc6d7b3c132b35`.

Environment: Python 3.11.9 on Windows 10 build 26200, AMD64. No observability instrumentation was
installed during the measurement.

```text
pytest tests/unit tests/contract tests/vendor --no-cov -p no:cacheprovider -q
730 passed in 24.60s

Gateway in-process mock, 50 independent ASGI lifespan + POST /chat + full SSE drain samples:
p50=412.609ms p95=452.215ms min=366.384ms max=526.112ms

Worker in-process mock, 50 claim/process/stop samples:
p50=1.196ms p95=1.737ms min=1.053ms max=3.043ms

Docker Compose/PostgreSQL Week 4 happy-path smoke:
PASS session_a_response_before_memory_model_release seconds=1.340 blocked_requests=1
PASS worker_completed_job durable_memory_count=1
PASS session_b_ltm_rewrite_to_kira recent_messages=0
```

These timing samples are local harness references for before/after comparison. They include test
fixture and lifecycle costs and are not production latency SLOs. The same test functions, host,
sample count, and warm/cold policy must be used for Phase 7 comparison.

The Compose smoke used the real Gateway, Worker, PostgreSQL, migrations, memory initialization, and
deterministic local provider doubles. It also cleaned its synthetic conversation and memory evidence
after success. The reported 1.340 seconds is one functional smoke observation, not a latency
distribution; before/after overhead remains anchored to the repeatable 50-sample in-process
measurements above.

## References

- [OpenTelemetry messaging span conventions](https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/)
- [OpenTelemetry Collector resiliency](https://opentelemetry.io/docs/collector/resiliency/)
- [Langfuse native OpenTelemetry ingestion](https://langfuse.com/integrations/native/opentelemetry)
- [Prometheus OTLP ingestion](https://prometheus.io/docs/guides/opentelemetry/)
- [Langfuse Kubernetes Helm deployment](https://langfuse.com/self-hosting/deployment/kubernetes-helm)
