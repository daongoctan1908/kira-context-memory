# Week 4 T4.9 — Gateway scheduling observability

## Metric contract

The Gateway exposes one dedicated Prometheus counter for asynchronous memory-job scheduling:

```text
kira_memory_job_schedule_total{outcome="scheduled|disabled|duplicate|error"}
```

The metric has exactly one bounded label, `outcome`. It never includes `user_id`, `session_id`,
`turn_id`, `conversation_id`, `boundary_message_id`, `event_id`, query text, response text, prompt,
credential, or exception message.

## Outcome semantics

| Outcome | Meaning |
| --- | --- |
| `scheduled` | Formation is enabled; a new completed turn committed with a non-null job event ID. |
| `disabled` | A completed turn was eligible for persistence, but formation was disabled by config. |
| `duplicate` | The transactional append reported an idempotent duplicate while formation was enabled. |
| `error` | The enabled atomic append failed, timed out, or returned a new turn without a job ID. |

Streams without trusted identity, clean downstream completion, or non-blank assistant text never
enter the persistence callback and therefore do not emit a scheduling outcome. This avoids calling
an ineligible stream `disabled` and keeps the counter scoped to actual scheduling decisions.

## Relationship to existing signals

`kira_conversation_write_total` remains the completed-turn persistence metric. A single completion
can therefore emit both a conversation-write outcome and one scheduling outcome. For example:

- formation off and successful insert: `write=inserted`, `schedule=disabled`;
- formation on and successful atomic insert: `write=inserted`, `schedule=scheduled`;
- formation on and transaction failure: `write=error`, `schedule=error`.

An enabled new-turn result without `memory_job_event_id` is treated as a store protocol failure for
scheduling observability. The Gateway emits `schedule=error` plus the existing sanitized degraded
signal `postgresql/postgres_write`; it does not add a synthetic answer.

## Acceptance commands

```powershell
uv run pytest tests/unit/infrastructure/test_context_telemetry.py `
  tests/unit/application/test_short_term_chat.py tests/integration/test_short_term_api.py --no-cov
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
