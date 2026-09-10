# Week 4 T4.16 Memory Job Operator CLI

## Outcome

T4.16 adds a PostgreSQL-only command-line boundary for inspecting memory-job queue state and
explicitly requeueing one dead event. The commands consume `MemoryJobQueuePort`; they do not call
Mem0, embedding, memory LLM, KiRa, or the Gateway, and they do not expose an HTTP mutation route.

The installed entry point is `kira-memory-jobs`. The equivalent source-tree invocation is
`python -m worker.job_admin`.

## Commands

```powershell
uv run kira-memory-jobs stats
uv run kira-memory-jobs list-dead --limit 50
uv run kira-memory-jobs requeue --event-id 00000000-0000-0000-0000-000000000000
```

`stats` emits one JSON object containing only aggregate pending, processing, completed, and dead
counts plus the optional oldest-pending age in seconds.

`list-dead` emits newest dead jobs in the deterministic order already owned by the PostgreSQL
adapter. Its default limit is 50 and its hard CLI bound is 1–1,000. Every item contains only:

- event ID;
- attempt and requeue counts;
- bounded error class name;
- creation and dead timestamps normalized to UTC.

It never returns user, session, conversation, turn, boundary-message, transcript, prompt, provider
payload, memory, credential, or raw exception data.

`requeue` requires one exact UUID. The adapter conditionally changes that row only when its current
status is `dead`; it resets `attempt_count` to zero, increments `requeue_count`, makes the job due
immediately, and clears lease, terminal, and error state. It neither deletes/recreates the event nor
calls a provider. There is no wildcard, filter-based, bulk, or automatic requeue path.

## Configuration and lifecycle

The CLI uses `MemoryJobAdminSettings`, which requires only `DATABASE_URL` and PostgreSQL
pool/connect/command/operation timeout settings. It does not load Worker provider requirements.
Each process creates a small pool by default (size 2, zero overflow), validates the exact Alembic
schema revision before executing the command, applies the configured operation timeout, and always
disposes an engine it owns.

The operator database role needs only the queue permissions appropriate to the invoked commands.
It does not need pgvector schema DDL privileges or provider credentials. A wrong schema revision
fails closed before any command runs.

## Output and exit codes

Successful command output is one compact, key-sorted JSON object on stdout. Invalid invocations and
sanitized runtime errors are one JSON object on stderr; supplied invalid values and raw exception
messages are never printed.

| Exit code | Meaning |
| --- | --- |
| 0 | Inspection succeeded, or the requested dead event was requeued |
| 1 | Unexpected internal CLI failure |
| 2 | Invalid invocation, configuration, or schema revision |
| 3 | PostgreSQL unavailable or queue operation/protocol failure |
| 4 | The supplied event ID does not currently identify a dead job |
| 130 | Operator interrupted the process |

Exit code 4 deliberately combines missing and non-dead rows. Automation can safely distinguish a
successful mutation from a no-op without learning additional job state.

## Acceptance evidence

Validated locally on 2026-09-10:

- focused operator CLI tests: 21 passed;
- full default suite: 588 passed, 63 skipped, with 93.12% total coverage;
- real PostgreSQL/pgvector marker: 61 passed, 1 existing opt-in semantic-provider gate skipped;
- Ruff lint, formatting, lock, and Git whitespace checks passed;
- Docker image `kira-context:0.4.0-t4.16` built successfully and contains the installed
  `kira-memory-jobs` entry point;
- image-level `stats` and bounded `list-dead` commands succeeded against real PostgreSQL while only
  `DATABASE_URL` was configured;
- image-level requeue of an unknown UUID returned sanitized `requeued=false` with exit code 4;
- invalid limit and missing configuration returned sanitized JSON with exit code 2;
- the runtime image and commands ran as the non-root `kira` user.

The real PostgreSQL test creates one dead job, verifies it appears without transcript or identity
data, requeues it exactly once, verifies the second request is a no-op, checks the reset pending
state and incremented requeue count, and removes the synthetic conversation/job afterward.

## Scope boundary

T4.16 does not add a public or Worker HTTP mutation endpoint, bulk requeue, automatic dead-letter
recovery, queue payload inspection, Mem0 administration, Compose Worker wiring, or final
Gateway-to-Worker provider E2E. Those deployment concerns remain separate from this operator
boundary.
