# Week 4 T4.12 — Memory job processing use case

## Outcome

`ProcessMemoryJobUseCase` is the application boundary for one already-claimed PostgreSQL memory
job. T4.12 does not claim jobs, start concurrent tasks, refresh leases, run cleanup, expose HTTP, or
handle process signals; those remain T4.13 onward.

For one `MemoryJob`, the use case:

1. passes its exact `CompletedTurnReference` to the existing `ProcessMemoryUseCase`;
2. reads the bounded snapshot through `boundary_message_id` from PostgreSQL;
3. delegates formation to native Mem0 V3 without restricting lifecycle actions;
4. records one lease-token-guarded queue transition;
5. returns only a low-cardinality outcome, lifecycle-event count, sanitized error class, and
   optional retry timestamp.

Success with zero lifecycle events is still a successful completed job. `ADD`, `UPDATE`, `DELETE`,
`NONE`, or any other valid native lifecycle result is counted as returned; the Worker does not
reinterpret Mem0's decision.

## Failure classification

| Classification | Typed failures | Queue action |
| --- | --- | --- |
| Retryable | conversation connection/operation; memory connection/operation/timeout | retry while attempts remain |
| Permanent | invalid boundary or conversation protocol; configuration/schema; memory protocol | dead immediately |
| Final attempt | any retryable failure on attempt 5 | dead, never schedule attempt 6 |
| Cancellation | task/process cancellation | no transition; keep lease for expiry/reclaim |
| Unexpected runtime error | unexpected `Exception` | bounded retry, then dead on final attempt |
| Queue/lease failure | complete/retry/dead transition fails or lease is stale | propagate; never attempt a second transition |

The default attempt-to-delay mapping is deterministic:

| Failed attempt | Next delay |
| --- | --- |
| 1 | 1 second |
| 2 | 5 seconds |
| 3 | 30 seconds |
| 4 | 120 seconds |
| 5 | dead-letter |

Retry timestamps are computed from an injected timezone-aware clock and persisted as absolute UTC
capable datetimes. The database remains authoritative for whether the current lease is still valid.

## Safety properties

- The exact event ID and lease token from the claim are used for every transition.
- Only exception class names are persisted or returned; exception messages, prompts, conversation
  text, provider payloads, credentials, and memory facts are excluded.
- A successful Mem0 call followed by a failed completion transition may be delivered again after
  lease expiry. This is the documented at-least-once boundary, not an exactly-once claim.
- Retry schedule shape is validated independently by both `WorkerSettings` and the use case.
- A claimed job whose attempt count exceeds the configured maximum is rejected as an invariant
  violation before any provider or queue mutation.

## Acceptance evidence

Focused tests cover the exact-boundary chain through the real `ProcessMemoryUseCase`, mixed native
lifecycle results, zero/positive event completion, every typed retryable and permanent error,
attempt-indexed delays, bounded unexpected exceptions, cancellation, stale
lease/queue transition failures, and constructor/job invariants. Full repository, PostgreSQL, Ruff,
and Docker gates remain required before the task commit is accepted.

Validated locally on 2026-09-09:

- focused application/Worker and PostgreSQL processing tests: 59 passed;
- full default suite: 509 passed, 59 skipped, 92.36% coverage;
- full PostgreSQL/pgvector marker: 57 passed, 1 existing opt-in semantic gate skipped;
- Ruff lint and format checks passed;
- Docker image `kira-context:0.4.0-t4.12` built and imported the packaged use case as non-root
  user `kira` without KiRa or query-rewriter configuration.
