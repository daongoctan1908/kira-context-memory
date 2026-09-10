# Week 4 T4.23 — Release acceptance for 0.4.1

## Outcome

`0.4.1` is the corrective Week 4 local release candidate. It retains the asynchronous PostgreSQL
queue and native Mem0 V3 extraction from `0.4.0`, but replaces T4.21's narrow exact-hash retry
assumption with event-scoped formation receipts.

Distribution metadata, Gateway OpenAPI, Worker metadata, Docker build argument, OCI label, and
Compose runtime tag use `0.4.1`. The internal memory package is pinned to
`viettel-mem0==2.0.20+viettel.3`, and memory schema version 2 adds the collection-scoped receipt
contract.

## Formation correction

- `memory_jobs.event_id` is propagated through the application and adapter as
  `formation_event_id`.
- An exact primary-key receipt lookup occurs before embedding, semantic top-k retrieval, and LLM
  extraction.
- All vector memories and the receipt commit in one psycopg transaction.
- A concurrent or reclaimed retry returns the first committed result, even if a hypothetical later
  extraction would paraphrase the fact or semantic search would miss it.
- A mid-batch vector failure rolls back both the vectors and receipt.
- Queue completion remains a separate idempotent transition, preserving at-least-once delivery.

SQLite history and entity links are derived best-effort side effects after the authoritative
memory/receipt commit. They are not advertised as transactionally idempotent.

## Build and schema verification

The image was built from the frozen root lock and inspected:

```powershell
docker compose -f compose.week4.yaml build gateway
docker compose -f compose.week4.yaml up -d --no-build --wait
```

Image and runtime inspection returned:

- OCI version `0.4.1`;
- non-root runtime user `kira`;
- installed application `0.4.1`;
- installed internal Mem0 `2.0.20+viettel.3`;
- memory schema metadata `2 | 2.0.20+viettel.3`.

The existing Week 4 schema was upgraded by the explicit `memory-init` job. Separate real-PostgreSQL
tests also initialized schema version 2 from empty state and simulated the only supported version
1 / `viettel.2` upgrade while preserving an existing vector. Unknown metadata tuples still fail
closed.

## Acceptance matrix

Validated locally on 2026-09-11:

| Gate | Evidence |
| --- | --- |
| Lock and quality | `uv lock --check`, root Ruff lint/format, diff check, and Compose config passed |
| Default suite | 629 passed, 69 skipped, coverage 92.42% |
| PostgreSQL integration | 67 passed, 1 explicit live-evaluation skip |
| Custom Mem0 focused regression | 150 passed, 3 subtests passed |
| Atomic receipt transaction | paraphrase conflict kept first result; mid-batch failure rolled back rows and receipt |
| T4.19 async happy path | response-before-formation, durable memory, and cross-session recall passed |
| T4.20 retry/dead/requeue | attempt 2 recovery, attempt 5 dead, and explicit requeue recovery passed |
| T4.21 crash before commit | zero memory/receipt before reclaim; attempt 2 committed once; provider calls 2 |
| T4.21 crash after commit | attempt 2 replayed receipt; one memory/receipt; provider calls 1 |
| T4.22 outage/readiness | Worker degraded/recovered; Gateway returned real KiRa SSE fallback |
| Runtime | Gateway, Worker, PostgreSQL, and provider mocks healthy on image `0.4.1` |
| Post-smoke state | queue rows, synthetic memories, and synthetic receipts all zero |
| Redis exclusion | no Compose service and no installed distribution named `redis` |

All end-to-end payloads and credentials were synthetic. Evidence output uses bounded status,
counts, lifecycle-event counts, and attempt numbers rather than conversation text.

## External gate status

Real internal KiRa, Qwen/vLLM, embedding, and memory-LLM endpoints were not configured in this
workspace, so production-provider acceptance is **NOT_RUN**, not passed. Image publication, Git
tagging, deployment, performance/SLO qualification, PostgreSQL HA/failover validation, and alert
routing remain release-management or environment-specific gates.
