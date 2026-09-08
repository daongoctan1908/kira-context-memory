# Week 3 Batch C2 — parallel LTM retrieval orchestration

## Delivered checkpoint

C2 extends `HandleChatUseCase` with an optional `LongTermMemoryPort` dependency:

```text
                         +-> PostgreSQL read_recent --------+
trusted user + current --|                                  |-> ContextBuilder
                         +-> Mem0 search(original current) --+   -> rewriter -> KiRa
```

Both contextual reads start concurrently. Search always uses the trusted principal's `user_id`
and the original current query, never a client-supplied identity or a rewritten query. The existing
completed-turn persistence path still stores the original user query and exact KiRa answer.

FastAPI lifecycle construction, Mem0 client ownership/close, dedicated LTM metrics, and production
runtime enablement are intentionally deferred to C3. With no injected LTM port, behavior is exactly
the Week 2 short-term baseline.

## Decision table

| PostgreSQL recent | LTM search | Rewriter behavior |
| --- | --- | --- |
| success, history exists | success, results exist | Rewrite with Recent + ranked LTM + Current |
| success, empty | success, results exist | Rewrite with LTM + Current for cross-session recall |
| success | typed failure or timeout | Continue with Recent + Current |
| success, empty | empty or failed | Bypass and send original Current |
| failure or timeout | any result | Discard LTM, bypass, and send original Current |
| success | success | Rewriter failure still sends original Current |

The PostgreSQL-failure rule is deliberately stricter than the LTM-failure rule. Recent conversation
is the source needed to establish the safe current session state; if it cannot be read, potentially
stale cross-session memory is not applied on its own.

## Concurrency and failure boundary

- PostgreSQL retains its existing total operation timeout.
- The use case enforces a separate total memory-search timeout even when an adapter has its own
  transport timeout.
- Typed `LongTermMemoryError` failures are graceful-degradation inputs.
- Unexpected programming errors propagate. The sibling read task is cancelled and awaited first,
  preventing an orphan task from continuing after the request fails.
- Request cancellation propagates and cancels both contextual reads; it is never converted into a
  dependency fallback.
- Invalid provider result shapes are discarded as unavailable optional LTM. Dedicated protocol
  logging/metrics are part of C3.

## Configuration contract

The injected use case accepts:

- `memory_search_top_k`: integer 1–10;
- `memory_search_threshold`: numeric 0–1;
- `memory_search_timeout_seconds`: positive numeric duration.

Results remain ranked by the memory provider. C1's `ContextBuilder` applies the final hard cap and
does not place LTM under the recent estimated-token budget.

## Acceptance coverage

Unit acceptance verifies:

- combined Recent + LTM context and LTM-only cross-session rewrite;
- trusted-user scoping and original-query search parameters;
- empty-context bypass;
- every typed memory failure and an enforced hanging-search timeout;
- PostgreSQL connection failure and timeout discarding successful LTM;
- simultaneous PostgreSQL + memory failure;
- actual concurrent start using a two-sided barrier;
- rewriter failure after LTM retrieval;
- no search without trusted identity;
- provider protocol violation fallback;
- unexpected-error sibling cancellation and request-cancellation propagation;
- validation of all search controls;
- unchanged Week 2 streaming and persistence behavior.

Run the focused gate:

```powershell
uv run pytest tests/unit/application/test_long_term_chat.py `
  tests/unit/application/test_short_term_chat.py `
  tests/unit/application/test_context_builder.py `
  tests/unit/application/test_rewrite_prompt.py --no-cov
```

Full Ruff, repository coverage, PostgreSQL integration, and container smoke remain required before
the C2 commit is accepted.

## Local evidence — 2026-09-08

- Focused C1/C2 application gate: 111 passed.
- Full repository suite: 370 passed, 18 environment-gated tests skipped; coverage 92.27% and
  `HandleChatUseCase` at 100%.
- Ruff lint and format check: passed.
- Full PostgreSQL/pgvector integration marker: 17 passed.
- Customized Mem0 pgvector provider gate: 91 passed.
- Docker image `kira-context:0.3.0` rebuilt; migration exited zero and the recreated Gateway and
  PostgreSQL containers are healthy.
- Synthetic socket E2E: all 8 Week 2 cases passed with completed-turn persistence and metrics.
- Real online Mem0 retrieval/model gate: **NOT RUN** because adapter lifecycle wiring is C3 and no
  approved model endpoint is configured in this synthetic stack.
