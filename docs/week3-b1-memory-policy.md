# Week 3 Batch B1 memory policy

## Delivered boundary

- Memory extraction policy is versioned in code as `kira-memory-policy-v1`.
- The approved taxonomy contains `USER_CONTEXT`, `ANALYSIS_PREFERENCE`,
  `USER_DEFINED_METRIC`, `USER_DEFINED_CONVENTION`, `TEMPORARY_FOCUS`, and
  `EPISODIC_ANALYSIS_CONTEXT`.
- Taxonomy values guide extraction only. They are not persisted as metadata and are not prefixed
  to memory text.
- Durable facts must be grounded in user messages. Assistant content can only resolve references,
  confirmations, or ellipsis and cannot be the sole factual source.
- Greetings, ordinary query entities, generated results, guesses, secrets, and inferred
  authorization are explicitly excluded.
- Formula-like user definitions preserve expressions, operators, units, names, and thresholds.
- Conversation messages are treated as untrusted data and cannot override the memory policy.
- `Mem0Adapter` supplies the versioned policy through `MemoryConfig.custom_instructions` without
  changing the vendored Mem0 engine.

## Deferred work

Batch B2 validates the policy with positive, negative, formula-preservation, and adversarial dev
cases. Process-memory orchestration, online retrieval, and cross-session integration remain later
checkpoints.

## Local checkpoint evidence

- Ruff format and lint checks pass.
- B1 policy and adapter tests: 27 passed.
- Full repository suite: 289 passed, 16 skipped, 91.63% coverage.
- Internal Mem0 pgvector provider suite: 91 passed.
- Docker image `kira-context:0.3.0` rebuilds successfully and contains policy v1.
- No vendored Mem0 source was changed in this batch.
