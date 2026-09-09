# Week 4 Batch B — Gateway scheduling acceptance

## Scope closed

Batch B completes T4.7–T4.10:

- `MEMORY_FORMATION_ENABLED` is off by default and independent from `LTM_ENABLED`;
- only a trusted, non-empty, cleanly completed KiRa stream requests memory-job scheduling;
- the completion callback starts at most once;
- completed conversation messages and the reference-only job commit in one PostgreSQL transaction;
- Gateway request/completion code never runs Mem0 formation, embedding, or extraction;
- scheduling exposes only the bounded outcomes `scheduled`, `disabled`, `duplicate`, and `error`.

## Regression matrix

| Scenario | Conversation | Memory job | SSE/metric expectation |
| --- | --- | --- | --- |
| Formation on, trusted clean completion | Committed | One pending row | `scheduled` |
| Formation off, trusted clean completion | Committed | None | `disabled` |
| Duplicate completion write | Unchanged | No duplicate row | `duplicate` |
| KiRa failure before/mid-stream | None | None | typed downstream error; no schedule sample |
| Client disconnect before clean EOF | None | None | source closed; no callback/sample |
| Empty/whitespace assistant output | None | None | no callback/sample |
| Missing trusted identity | None | None | current-query SSE only; no sample |
| PostgreSQL/job insert failure | Rolled back | None | original KiRa frames unchanged; `error` |

The PostgreSQL regression test deliberately causes a job primary-key conflict after a valid seed
job. It verifies that the target conversation and both messages roll back while the client still
receives exactly the KiRa SSE frame. Synthetic rows are scoped by unique session IDs and removed by
test cleanup.

## Security and architecture assertions

- Queue rows contain an immutable assistant boundary reference, not conversation text.
- Metrics use only fixed outcome labels; no identity, query, session, turn, boundary, or event ID.
- Formation scheduling never depends on Mem0/provider availability in the Gateway.
- `/ready` semantics and the Week 1 public `/chat` SSE contract remain unchanged.

## Acceptance commands

```powershell
uv run pytest tests/unit/application/test_short_term_chat.py `
  tests/unit/presentation/test_sse.py tests/integration/test_gateway_api.py `
  tests/integration/test_short_term_api.py --no-cov

$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run pytest tests/integration/postgres/test_conversation_store.py `
  -k "gateway" --no-cov

uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The isolated Compose acceptance stack now builds and tags `kira-context:0.4.0-batch-b`. Run it with
formation explicitly enabled; its PostgreSQL credentials and conversation content are synthetic:

```powershell
$env:MEMORY_FORMATION_ENABLED="true"
docker compose -p kira-context-week4-batch-b -f compose.week2-smoke.yaml up -d --build --wait
uv run python scripts/smoke_gateway.py --gateway-url http://127.0.0.1:18000 `
  --session-id week4-batch-b-smoke --message "synthetic completed turn"
docker compose -p kira-context-week4-batch-b -f compose.week2-smoke.yaml down -v
Remove-Item Env:MEMORY_FORMATION_ENABLED
```

Worker claiming/processing, retries, dead-letter policy, and worker telemetry remain outside Batch
B and start in the next Week 4 batch.
