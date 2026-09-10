# Week 4 T4.19 — Async happy-path E2E

## Outcome

T4.19 proves the synthetic asynchronous memory happy path across the deployed Week 4 stack:

`Session A SSE complete → PostgreSQL job → Worker/Mem0 formation → Session B LTM search → Rewriter → KiRa`

The acceptance deliberately blocks the memory-LLM provider before Session A starts. Gateway still
finishes the KiRa SSE response while the Worker job is `processing`; only then does the test release
the provider. This is the causal proof that memory formation is not on the request latency path, not
merely a comparison of two wall-clock timings.

## Deterministic scenario

Every run generates an isolated synthetic run ID, two new session IDs, one marked memory fact, and
one follow-up. The provider fixtures recognize only the `T4_E2E_MEMORY:` and
`T4_E2E_FOLLOW_UP:` acceptance markers. They do not replace or reinterpret the production memory
policy.

The runner performs these gates in order:

1. Require Gateway, Worker, KiRa, Rewriter, and memory-LLM health/readiness.
2. Reset content-free provider evidence and put memory-LLM into blocked mode.
3. Complete Session A through public `POST /chat` and consume the SSE answer fully.
4. Observe one blocked memory-LLM request and the matching PostgreSQL job in `processing` state.
5. Release memory-LLM, wait for the job to become `completed`, and require exactly one persisted
   memory for the synthetic fact and user.
6. Send the follow-up in Session B, which has no recent conversation messages from Session A.
7. Require Rewriter evidence with one LTM item, zero recent items, and the expected output hash.
8. Require KiRa evidence showing Session A received the original query and Session B received the
   rewritten query.
9. Require the Gateway scheduling/search/rewrite success series and Worker processing success
   series to be present.
10. Wait for Session B formation to finish, then remove both synthetic conversations, their jobs,
    and the synthetic memory.

Current query, memory fact, prompt text, response text, session ID, turn ID, event ID, and user ID
are never returned by the fixture evidence endpoints. Evidence is limited to hashes, bounded counts,
and control state. The smoke command prints only stage names, duration, and counts.

## Runbook

Start the stack from T4.18 and run the acceptance from the host:

```powershell
docker compose -f compose.week4.yaml up -d --no-build --wait
uv run python -m scripts.smoke_week4_async
docker compose -f compose.week4.yaml exec -T worker kira-memory-jobs stats
```

`WEEK4_DATABASE_URL` can override the loopback Compose database URL. Provider and service URLs,
plus the bounded timeout, have explicit CLI overrides; `--help` lists them. This runner is for the
local synthetic stack only.

Cleanup runs only after every acceptance assertion and both formation jobs succeed. A failed run
keeps database evidence for diagnosis but always attempts to release the blocked provider and close
the database pool. Use `docker compose -f compose.week4.yaml down -v` for a full disposable reset.

## Acceptance evidence

Validated locally on 2026-09-10:

- Session A SSE completed in 1.007 seconds while memory-LLM reported one blocked request and the
  durable job was still `processing`;
- after release, Worker completed the job and exactly one matching durable memory existed;
- Session B used one LTM item and zero recent messages at Rewriter;
- Rewriter output hash exactly matched the second KiRa query hash;
- Gateway scheduling, memory search, and rewrite success metrics plus Worker processing success
  metric were present;
- post-success cleanup left queue stats at zero for pending, processing, completed, and dead;
- provider/control and smoke helper tests: 15 passed;
- full default suite: 603 passed, 65 skipped, with 93.12% coverage;
- Ruff lint and formatting checks passed.

## Scope boundary

T4.19 validates only the deployed happy path and its asynchronous boundary. It does not exercise
retry/dead/requeue behavior, process-crash recovery, dependency-outage dashboards, real KiRa/Qwen
or memory providers, or production performance. Those remain T4.20–T4.23.
