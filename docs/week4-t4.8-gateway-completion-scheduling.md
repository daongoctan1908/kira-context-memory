# Week 4 T4.8 — Gateway completion scheduling

## Delivered flow

When `MEMORY_FORMATION_ENABLED=true`, the Gateway carries a boolean scheduling request into the
existing completed-stream persistence callback:

```text
trusted identity + clean KiRa EOF + non-blank assistant text
  -> completion callback exactly once
  -> ConversationStorePort.append_turn(..., schedule_memory=true)
  -> PostgreSQL transaction writes user + assistant + reference-only pending job
```

The callback persists the original user query and exact concatenated assistant text. It never
persists the rewritten query. PostgreSQL remains the only commit boundary; the Gateway does not
perform extraction, embedding, or `LongTermMemoryPort.process_memory()`.

## Eligibility matrix

| Condition | Persist turn | Request memory job |
| --- | --- | --- |
| Trusted identity, clean EOF, assistant text, flag enabled | Yes | Yes |
| Trusted identity, clean EOF, assistant text, flag disabled | Yes | No |
| Missing trusted identity | No | No |
| KiRa failure before or during stream | No | No |
| Client disconnect/explicit close before clean EOF | No | No |
| Empty or whitespace-only assistant stream | No | No |

The completion guard is set before awaiting persistence. Even if clean exhaustion is observed by
more than one consumer, the callback can begin only once. Cancellation still propagates and is not
converted into a successful completion.

## Failure semantics

The existing availability-first contract is unchanged:

- a PostgreSQL write/job-insert failure is observed as degraded persistence and does not append a
  synthetic answer to the already streamed KiRa content;
- cancellation during persistence propagates so the in-flight PostgreSQL transaction can roll
  back;
- a memory job contains only its boundary reference and operational fields, never conversation
  text or prompt data;
- Mem0 construction for online retrieval remains governed independently by `LTM_ENABLED`.

Scheduling outcome counters are intentionally deferred to T4.9. The existing conversation-write
metric continues to report only persistence outcome.

## Acceptance evidence

Focused tests cover successful enabled/disabled scheduling, exact-once completion, missing
identity, pre-stream/mid-stream failure, explicit close, empty output, write failure, cancellation,
Gateway non-invocation of Mem0 formation, and the real PostgreSQL atomic turn/job path.

```powershell
uv run pytest tests/unit/application/test_short_term_chat.py `
  tests/unit/presentation/test_sse.py tests/integration/test_gateway_api.py --no-cov

$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run pytest tests/integration/postgres/test_conversation_store.py `
  -k "gateway_completed_stream_persists_and_schedules_atomically" --no-cov
```
