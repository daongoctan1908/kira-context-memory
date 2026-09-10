# Week 4 T4.23 — Release acceptance for 0.4.0

## Outcome

T4.23 closes the Week 4 asynchronous-memory baseline as local release candidate `0.4.0`.
Distribution metadata, Gateway OpenAPI, Worker metadata, Docker build argument, OCI image label, and
Compose runtime tag now use the same version. The final Compose stack contains PostgreSQL/pgvector,
Gateway, Worker, migrations, memory initialization, and deterministic provider doubles; it has no
Redis service or Redis runtime dependency.

## Release-gate correction

The first sequential replay of T4.19–T4.22 exposed a real PostgreSQL startup race after T4.22
restored the database. asyncpg can raise raw `CannotConnectNowError` with SQLSTATE `57P03` while the
server is still starting. The queue adapter previously mapped SQLSTATE connection failures only
when wrapped in SQLAlchemy `DBAPIError`, so that raw driver exception stopped the Worker runner.

The final implementation catches asyncpg `PostgresError` at every queue operation boundary and
maps connection SQLSTATEs to `MemoryJobQueueConnectionError`. The runner therefore applies its
bounded database backoff and continues polling. A unit regression exercises the raw `57P03` path;
the complete deployed outage gate then passes without restarting the Worker.

## Pristine build and migration verification

The disposable Compose volume was verified as owned by project `kira-context-week4`, removed with
the old stack, and recreated from empty state. The final image was built from the frozen lock:

```powershell
docker compose -f compose.week4.yaml down -v
docker compose -f compose.week4.yaml build gateway
docker compose -f compose.week4.yaml up -d --no-build --wait
```

Image inspection returned OCI version `0.4.0` and runtime user `kira`. In-container inspection
returned package, Gateway, and Worker version `0.4.0`. On a separate disposable database, Alembic
successfully ran `upgrade head → downgrade base → upgrade head` and finished at revision
`20260908_0003`; the database was then dropped.

## Acceptance matrix

Validated locally on 2026-09-10:

| Gate | Evidence |
| --- | --- |
| Lock and quality | `uv lock --check`, Ruff lint/format, and merged Compose config passed |
| Default suite | 627 passed, 65 skipped, coverage 93.16% |
| PostgreSQL integration | 63 passed, 1 explicit live-evaluation skip, disposable DB removed |
| Custom Mem0 pgvector regression | 91 passed |
| T4.19 async happy path | response-before-formation, durable memory, cross-session recall passed |
| T4.20 retry/dead/requeue | attempt 2 recovery, attempt 5 dead, explicit requeue recovery passed |
| T4.21 crash/lease recovery | post-write SIGKILL and attempt 2 lease reclaim passed |
| T4.22 outage/readiness | Worker degraded/recovered; Gateway returned real KiRa SSE fallback |
| Final runtime | migration/init exited 0; Gateway, Worker, PostgreSQL and mocks healthy |
| Queue state | pending, processing, completed, and dead all zero after acceptance |
| Redis exclusion | no Compose service and no installed distribution named `redis` |

All end-to-end payloads and credentials were synthetic. Evidence output uses bounded status,
counts, hashes, lifecycle-event counts, and attempt numbers rather than conversation text.

## External gate status

Real internal KiRa, Qwen/vLLM, embedding, and memory-LLM endpoints were not configured in this
workspace, so production-provider acceptance is **NOT_RUN**, not passed. Image publication, Git
tagging, deployment, performance/SLO qualification, PostgreSQL HA/failover validation, and alert
routing remain release-management or environment-specific gates; T4.23 does not perform them.
