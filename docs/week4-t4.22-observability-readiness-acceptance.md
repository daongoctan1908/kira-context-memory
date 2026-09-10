# Week 4 T4.22 — Outage observability and readiness acceptance

## Outcome

T4.22 verifies the deployed PostgreSQL-outage contract through the synthetic Week 4 stack. The
test stops the real Compose PostgreSQL container; it does not monkeypatch application code.

During the outage:

- Worker `/health` remains 200 because its event loop is alive;
- Worker `/ready` becomes 503 because queue access is unavailable;
- Worker metrics expose database availability `0` and a positive retry backoff;
- Gateway `/health` and `/ready` remain 200 because contextual capabilities are soft dependencies;
- Gateway still proxies the exact current query to KiRa and returns the real downstream SSE text;
- failed conversation persistence cannot create a partial turn or memory job;
- Gateway counters and safe structured logs expose each degraded boundary without message content.

After PostgreSQL restarts, the same running Gateway and Worker recover without manual process
restart. Worker readiness returns to 200, queue database availability returns to `1`, and backoff
returns to `0`.

## Deterministic sequence

1. Require Gateway and Worker health/readiness plus Worker queue availability.
2. Snapshot the relevant Gateway counters and reset mock-KiRa request evidence.
3. Run `docker compose stop postgres` directly, with a bounded subprocess timeout.
4. Wait for Worker health 200, readiness 503, queue availability 0, and positive DB backoff.
5. Require Gateway health/readiness 200 and complete one chat request.
6. Require mock KiRa to receive the SHA-256 hash of the original current query exactly once.
7. Require one increment for PostgreSQL read/write degradation, write failure, memory schedule
   failure, memory search failure, and rewrite bypass.
8. Inspect Gateway/Worker Compose logs for the allowlisted operation and fallback fields while
   rejecting the synthetic message, session, user, provider credential, and database password.
9. In `finally`, restart PostgreSQL, wait for both runtime contracts to recover, and require that
   the failed write left no conversation row.

## Runbook

Start the base stack, then run the self-restoring outage acceptance:

```powershell
docker compose -f compose.week4.yaml up -d --no-build --wait
uv run python -m scripts.smoke_week4_observability
docker compose -f compose.week4.yaml ps
```

The script accepts explicit Gateway, Worker, mock-KiRa, database, Compose-file, and timeout
arguments. Docker Compose is invoked without an intermediate shell. Subprocess output is retained
only for assertions and is never emitted on a failure path.

## Acceptance evidence

Validated locally on 2026-09-10:

- PostgreSQL was stopped at the container boundary and restored by the test;
- Worker reported health 200, readiness 503, queue availability 0, and active database backoff;
- Gateway reported health/readiness 200 and returned the expected KiRa SSE response;
- mock KiRa received the original-query hash exactly once;
- all six Gateway degradation/outcome counters advanced by at least one;
- logs contained the required safe operation/fallback fields and none of the five sentinels;
- recovery restored Worker readiness, queue availability 1, zero database backoff, and Gateway
  readiness;
- no conversation row survived the failed transactional persistence boundary.
- smoke/provider helper regression: 66 passed;
- full default suite: 625 passed, 65 skipped, with 93.12% coverage;
- Ruff lint/format and merged Compose configuration checks passed.

## Scope boundary

This gate proves local runtime behavior against the deterministic provider stack. It does not
claim production PostgreSQL failover timing, real KiRa/Qwen/Mem0 provider quality, alert-manager
routing, or dashboard deployment. Final image versioning and the complete release matrix belong
to T4.23.
