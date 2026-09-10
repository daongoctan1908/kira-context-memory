# Week 4 T4.18 Compose Stack

## Outcome

T4.18 introduces `compose.week4.yaml`, an isolated synthetic-only deployment of every process
needed by the Week 4 asynchronous memory flow. PostgreSQL/pgvector, application migration,
pgvector memory initialization, Gateway, Worker, KiRa stub, query-rewriter stub, embedding stub,
and memory-LLM stub run as separate Compose services. Redis is not present.

This task establishes deployment wiring and startup order. It deliberately does not claim the
Session A → Worker → Session B recall behavior owned by T4.19.

## Service topology

| Service | Type | Dependency/startup contract | Loopback port |
| --- | --- | --- | --- |
| `postgres` | long-running | PostgreSQL 16 plus pgvector; healthy before DDL jobs | `15433` |
| `migrate` | one-shot | Runs `alembic upgrade head`; must exit 0 | none |
| `mock-embedding` | long-running | OpenAI-compatible `/v1/embeddings`, fixed 3-dimensional vectors | `18124` |
| `memory-init` | one-shot | Waits for migration and embedding health, then runs `python -m worker.memory_admin init`; must exit 0 | none |
| `mock-kira` | long-running | Deterministic authentication and SSE response | `18122` |
| `mock-rewriter` | long-running | Deterministic OpenAI-compatible rewrite contract | `18123` |
| `mock-memory-llm` | long-running | Deterministic native-Mem0 JSON extraction envelope | `18125` |
| `gateway` | long-running | Waits for both DDL jobs and all of its provider mocks; Compose health uses `/ready` | `18000` |
| `worker` | long-running | Waits for both DDL jobs plus embedding and memory-LLM health; Compose health uses `/ready` | `18001` |

All published ports bind to `127.0.0.1`. Gateway and Worker share the same immutable image but
start through separate commands and own separate dependency lifecycles. Worker uses
`python -m worker.main`; Gateway retains the image default command. Both run as the non-root
`kira` user.

## Deterministic provider boundary

The Compose provider apps are test fixtures mounted read-only into dedicated containers. They are
not fallback business services and must never be deployed outside controlled local acceptance.

- Embedding accepts the admin dimension probe and Mem0 single/batch requests for the pinned
  `local-embedding-stub` model. It always returns stable non-zero vectors of dimension 3.
- Memory LLM validates the OpenAI-compatible model, temperature, output size, and JSON response
  format used by native Mem0 V3. It extracts only synthetic user content prefixed with
  `T4_E2E_MEMORY:` and otherwise returns an empty `memory` list.
- The query-rewriter and KiRa stubs preserve their existing Week 2 contracts and now expose
  `/health` for Compose orchestration.

The marker is an acceptance-fixture protocol, not a production memory-extraction rule. Production
continues to use the Week 3 policy and native Mem0 V3 lifecycle behavior.

## Startup and cleanup

Build the shared image first so Compose never needs a registry image with this local tag, then
start and wait for every runtime service:

```powershell
docker compose -f compose.week4.yaml build gateway
docker compose -f compose.week4.yaml up -d --no-build --wait
docker compose -f compose.week4.yaml ps -a
```

Expected terminal state:

- `migrate` and `memory-init`: `Exited (0)`;
- PostgreSQL, Gateway, Worker, and all four provider mocks: `healthy`.

Useful probes:

```powershell
Invoke-RestMethod http://127.0.0.1:18000/health
Invoke-RestMethod http://127.0.0.1:18000/ready
Invoke-RestMethod http://127.0.0.1:18001/health
Invoke-RestMethod http://127.0.0.1:18001/ready
docker compose -f compose.week4.yaml exec -T worker kira-memory-jobs stats
```

Stop while retaining the synthetic PostgreSQL volume:

```powershell
docker compose -f compose.week4.yaml down
```

Reset all Week 4 synthetic state when a pristine acceptance run is required:

```powershell
docker compose -f compose.week4.yaml down -v
```

The latter permanently removes only the named `kira-context-week4_week4-postgres-data` development
volume. It must not be used against a production Compose project.

## Configuration and safety

The stack is self-contained and does not consume values from a developer `.env`. Its fixed database
and stub credentials are explicitly local-only. No real KiRa, Qwen, embedding, memory-LLM, or user
data is used. Runtime image contents still exclude tests; Compose mounts only `./tests` read-only
into mock containers. Gateway and Worker receive only the dependency settings they own.

`MEMORY_FORMATION_ENABLED=true` and `LTM_ENABLED=true` are enabled in the Gateway so later Batch D
tasks can exercise the complete topology. This does not move formation into the request path: a
completed Gateway turn only schedules a PostgreSQL job, and the Worker remains the sole Mem0
formation caller.

## Acceptance evidence

Validated locally on 2026-09-10:

- `docker compose config --quiet` passed and listed exactly 9 services with no Redis;
- image `kira-context:0.4.0-t4.18` built with the expected version label and non-root `kira` user;
- migration applied through revision `20260908_0003` and exited 0;
- memory initialization created schema version 1 with model `local-embedding-stub`, dimension 3,
  and exited 0;
- six long-running application/provider containers plus PostgreSQL became healthy;
- Gateway and Worker `/health` and `/ready` returned 200;
- all four provider `/health` endpoints returned 200;
- Worker CLI reported an empty durable queue and Worker metrics reported active runner plus
  available queue database;
- deterministic provider unit contracts: 6 passed.
- full default suite: 594 passed, 65 skipped, with 93.12% coverage.

The stack is intentionally left running after acceptance so T4.19 can use the same clean topology.

## Scope boundary

T4.18 does not send a chat request, assert response latency independence, process a real formation
job, verify cross-session recall, exercise retry/dead/requeue, simulate process crashes, or certify
observability under outage. Those behaviors remain T4.19–T4.22. Final image `0.4.0`, complete test
regression, and release evidence remain T4.23.
