# Observability data policy

Status: Phase 0 complete. This is the default policy for the internal pilot. Code and deployment
from later phases must enforce it before masked chat content is allowed into Langfuse.

## Data destinations

| Data | Destination | Default retention | Content policy |
| --- | --- | --- | --- |
| Traces and AI observations | Internal Langfuse OSS | 14 days | Masked and bounded content is allowed |
| Operational metrics | Prometheus | 30 days | Low-cardinality numeric data only |
| Application/container logs | Loki | 14 days | No chat, prompt, response, or provider body |
| Collector local queue/WAL | Encrypted persistent volume | Shortest operational window, maximum 24 hours | Same classification as the destination signal |

The pilot does not archive traces to datasets or a long-term bucket automatically. A Langfuse
dataset has a separate lifecycle and cannot be used to bypass this policy.

## Identity and content classification

### Allowed without masking

- random application identifiers: `correlation_id`, `turn_id`, `event_id`, `conversation_id`, and
  `boundary_message_id`;
- OTel `trace_id`/`span_id` and W3C propagation fields;
- bounded KiRa request/message IDs after validation as opaque internal IDs;
- service, version, environment, operation, stage, outcome, dependency, error class, fallback,
  attempt/requeue count, HTTP status class, durations, counts, model name, and non-secret model
  settings;
- prompt/rewrite policy version or cryptographic hash; and
- token usage reported by a provider, clearly separated from local token estimates.

### Pseudonymize before export

- `user_id` becomes `HMAC-SHA256(key, "user:" + user_id)`;
- Langfuse session identity becomes
  `HMAC-SHA256(key, "session:" + user_id + ":" + session_id)`; and
- any provider ID discovered to encode a user or phone identity uses the same HMAC strategy rather
  than being exported raw.

The HMAC key is a Kubernetes Secret with an explicit rotation procedure. Hashing without a secret
key is not accepted because predictable user/session identifiers could be enumerated.

### Always forbidden

- authorization headers, cookies, Basic/Bearer credentials, API keys, runtime tokens, passwords,
  DSNs, database credentials, Secret values, and environment dumps;
- raw SQL bind values and arbitrary provider URLs/query strings;
- embedding vectors;
- raw Python exception text or provider response bodies outside the approved masked AI fields;
- raw `user_id` or `session_id`; and
- W3C baggage or arbitrary client-supplied tracing attributes.

## Masked AI observations

The pilot stores masked content by default only for these reviewed fields:

- current user query and selected recent-message window;
- retrieved memory text actually considered/selected, plus its internal memory ID and score;
- rewrite input and rewritten query;
- query sent to KiRa and bounded final/partial KiRa text;
- Mem0 formation message window, existing-memory text, extraction prompt/output, and extracted
  facts.

Masking runs synchronously before a value becomes an OTel span attribute or enters an exporter
queue. The Collector repeats structural filtering but is not the first protection layer.

The masking implementation must cover at least:

- email addresses and phone/MSISDN-like values;
- IMSI, ICCID, national identity, account, subscriber, contract, and payment identifiers;
- IP/MAC addresses where present in customer content;
- JWTs, authorization schemes, API-key patterns, private keys, passwords, DSNs, and configured
  runtime secrets; and
- explicitly marked confidential values in telecom test fixtures.

Long digit sequences are masked conservatively. Percentages, dates, KPI names, cell/site labels,
and formulas may remain only when no customer/subscriber identifier pattern matches. Phase 1 tests
must include domain examples so redaction does not silently turn `98%`, a date, or a KPI formula
into unusable evidence.

Each content field has a byte/character limit. Truncated observations carry `truncated=true` and
the original byte count; they never imply that the stored snapshot is complete. If masking throws,
times out, or cannot classify a field, that content field is omitted and `content_omitted` records
the bounded reason. The business operation continues.

Generic HTTP, database, ASGI, and exception instrumentation has content capture disabled. Content
is attached only by reviewed adapters at the retrieval, rewrite, KiRa, and Mem0 boundaries.

## Trace and log fields

Trace-level Langfuse attributes that must be repeated as required for filtering are:

- pseudonymous `langfuse.user.id` and `langfuse.session.id`;
- environment, release/version, and bounded tags; and
- searchable metadata for `correlation_id`, `turn_id`, `event_id`, and `origin_trace_id` when
  available.

The intrinsic OTel trace ID remains the trace ID. `origin_trace_id` is searchable linkage metadata
for a Worker trace and never replaces `correlation_id`.

Operational logs contain IDs and outcomes but no AI content. The stable schema is:

```text
timestamp severity event service.name deployment.environment
trace_id? span_id? correlation_id? turn_id? event_id?
operation? dependency? outcome? error_class? fallback_mode? attempt_count?
```

Logger messages are static developer-authored text. Dynamic exception/provider text is not
interpolated into logs.

## Cardinality rules

Metric attributes may contain only reviewed bounded enums:

- service and deployment environment;
- stage/operation/dependency;
- outcome/status;
- claim kind and cleanup status.

The following are forbidden on metrics and Loki index labels: correlation, trace, span, user,
session, conversation, turn, boundary, provider request/message, memory, and job event IDs; model
input/output; exception text; URL; SQL; and arbitrary error codes.

High-cardinality IDs are allowed as Langfuse span metadata and unindexed JSON log fields. Loki
resource labels are limited to standard Kubernetes/service identity. Dashboards search an ID in
log content rather than promoting it to a label.

## Sampling

The internal pilot records 100% of instrumented business traces. It excludes:

- liveness, readiness, and metrics endpoints;
- empty queue polling loops;
- routine cached queue snapshots unless they fail or change readiness; and
- exporter/collector self-scrapes.

Metrics are never trace-sampled. Operational logs follow severity and rate-limit rules rather than
trace sampling. A missing or non-recording trace does not disable application correlation.

Moving to lower sampling is a separate capacity decision based on measured volume, storage, and
debug coverage. Head sampling cannot guarantee preservation of semantically wrong HTTP-200 AI
answers. No plan may claim “all bad outputs are retained” without a quality signal or tail-sampling
design that demonstrably identifies them.

## Retention and deletion

Langfuse OSS stores data indefinitely by default and does not provide the selected self-hosted
retention feature without Enterprise. Phase 6 therefore owns an hourly cleanup CronJob that uses
the supported public API to delete traces older than 14 days in bounded pages.

Requirements for the cleanup job:

- explicit project allowlist and dry-run mode;
- cutoff computed in UTC and fixed for the whole run;
- bounded page and deletion batch size;
- request timeout, retry/backoff for retryable responses, and safe resume;
- idempotent rerun and no direct ClickHouse/PostgreSQL table deletion;
- metric/log evidence for last success, failures, examined/deleted counts, and oldest remaining
  trace; and
- follow-up query because trace deletion is asynchronous.

Loki enforces a 14-day lifecycle and Prometheus 30 days. Object storage versions, persistent
Collector queues, snapshots, and backups must not retain classified telemetry beyond their stated
window. The pilot does not require telemetry backup; infrastructure snapshots, if enabled by the
cluster, must be encrypted and expire within the same 14-day maximum.

Deletion is asynchronous, so “14 days” is a cutoff policy and scheduled processing target rather
than a promise of deletion at an exact second. Cleanup backlog must alert before it becomes an
unbounded retention path.

## Access and network controls

- Langfuse, Grafana, Prometheus, Loki, and Collector ingestion remain internal to the cluster or
  protected internal ingress with TLS.
- End users never receive Langfuse project keys or Collector credentials.
- Applications receive only the Collector service endpoint. The Collector owns the Langfuse
  ingestion key in a Kubernetes Secret.
- Langfuse UI access is limited to the engineering/operations group during pilot. Shared accounts
  are not accepted.
- PostgreSQL monitoring uses a read-only monitoring role and never shares the application's admin
  credentials.
- NetworkPolicies restrict application egress to declared dependencies and restrict telemetry
  ingestion/admin endpoints to named namespaces/service accounts.
- Secrets are referenced by name; no secret value is committed to Git, Compose output, logs, span
  attributes, or Helm values checked into the repository.

## Failure behavior

Telemetry operations are fail-open and resource-bounded:

- no request or Worker attempt awaits remote telemetry export;
- no telemetry error changes a typed business error, fallback, retry delay, lease, queue
  transition, formation receipt, HTTP/SSE response, readiness, or liveness;
- masking failure omits only the content field;
- SDK/Collector queue exhaustion drops telemetry and increments a bounded operational signal;
- shutdown flush has a deadline and occurs after business cleanup; and
- repeated telemetry failures are rate-limited to prevent log storms.

Collector pipelines for traces, metrics, and logs have independent sending queues. A Langfuse
outage cannot consume the metrics/log queue budget. Persistent Collector storage reduces loss
during backend restart but does not create a business-delivery guarantee.

## Required privacy tests

Before masked-content capture is enabled outside synthetic development data, tests must prove:

1. every forbidden credential/identity fixture is absent from exported span payloads and logs;
2. correlation, trace, turn, and event IDs stay separate and searchable;
3. HMAC user/session values are stable with one key and change after controlled key rotation;
4. concurrent users cannot leak context or masked content into one another's traces;
5. truncation and mask failure omit safely without changing the business result;
6. no high-cardinality identifier appears in metric attributes or Loki labels; and
7. telemetry disabled, sampled out, Collector down, and Langfuse down all preserve application
   behavior.

Production-like customer data remains prohibited until these tests pass with reviewed synthetic
telecom examples and the output is inspected in the actual self-hosted Langfuse UI.

## References

- [Langfuse OpenTelemetry attribute mapping](https://langfuse.com/integrations/native/opentelemetry)
- [Langfuse masking guidance](https://langfuse.com/docs/observability/sdk/advanced-features)
- [Langfuse data deletion](https://langfuse.com/docs/administration/data-deletion)
- [OpenTelemetry Collector resiliency](https://opentelemetry.io/docs/collector/resiliency/)
