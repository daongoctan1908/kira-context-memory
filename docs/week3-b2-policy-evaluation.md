# Week 3 Batch B2 — memory-policy acceptance gate

> Historical v2 checkpoint. The active policy is v5 and the unchanged corpus is v4; see
> [source-time grounding](memory-temporal-grounding.md) for changes and current validation.

## Delivered boundary

Batch B2 turns the B1 extraction rules into an executable, versioned acceptance gate. It does not
call `Memory.add()` and cannot write to pgvector. The gate sends the exact Mem0 V3 system prompt,
prompt sections, and KiRa `custom_instructions` to the configured OpenAI-compatible memory model,
then scores only its JSON response in memory.

- Policy version: `kira-memory-policy-v2`.
- Evaluation corpus version: `kira-memory-policy-eval-v2`.
- Source handling follows native Mem0 V3: user and assistant messages can both contribute durable
  information. The policy preserves attribution instead of requiring every detail to be repeated
  in a user message.
- Every model call uses `temperature=0`, `stream=false`, and JSON response mode.
- Conversation messages occur before the custom policy in the generated prompt. The policy occurs
  before the output marker, and explicitly wins over conflicting general Mem0 guidance.
- Evidence contains case IDs, outcome, reason codes, fact count, and latency only. It never prints
  the input conversation, extracted memory text, API key, or raw provider response.
- Model, URL, API key, and timeout come from environment variables. The API key cannot be supplied
  on the command line.

## Acceptance matrix

The corpus contains 18 completed synthetic conversations.

| Dimension | Required behavior |
| --- | --- |
| `USER_CONTEXT` | Store explicit user responsibility and business scope. |
| `ANALYSIS_PREFERENCE` | Store explicit reusable comparison/presentation preferences. |
| `USER_DEFINED_METRIC` | Store the definition and preserve formula and threshold exactly. |
| `USER_DEFINED_CONVENTION` | Store the user convention and preserve names such as `MTD`. |
| `TEMPORARY_FOCUS` | Store the focus together with its explicit validity window. |
| `EPISODIC_ANALYSIS_CONTEXT` | Store only an analytical conclusion explicitly confirmed by the user. |
| Assistant-reference confirmation | Resolve the adopted proposal without requiring the user to repeat its details. |
| Confirmed assistant threshold | Preserve `threshold = 10%` when the user answers `Đúng`. |
| Mixed context and query | Keep the explicit user context but exclude ordinary query entities/time. |
| Greeting/filler | Extract no memory. |
| Ordinary query entity | Extract no memory. |
| Assistant KPI/result | Extract no memory. |
| Assistant guess | Extract no memory. |
| Synthetic credential | Extract no memory. |
| Inferred authorization | Extract no memory. |
| Conversation prompt injection | Extract no memory. |
| Assistant claim plus unrelated continuation | Do not treat filler as explicit confirmation. |
| Unconfirmed recommendation | Extract no memory. |

All cases must pass. In addition to the expected fact-count range, the scorer checks required
semantic terms, case-sensitive formula/name fragments, accepted source-form or ISO dates,
forbidden query contamination, absence of taxonomy prefixes/metadata, and valid native V3 source
attribution (`user` or `assistant`). A malformed OpenAI or Mem0 JSON envelope is a
dependency/protocol failure, not a passing negative case.

## Commands

Run the deterministic offline contract and scorer tests:

```powershell
uv run pytest tests/unit/scripts/test_check_live_memory_policy.py `
  tests/unit/application/test_memory_policy.py `
  tests/vendor/test_mem0_pristine_contract.py --no-cov
```

Run the write-free live model gate after configuring `MEMORY_LLM_BASE_URL`, `MEMORY_LLM_MODEL`,
optional `MEMORY_LLM_API_KEY`, and `MEMORY_OPERATION_TIMEOUT_SECONDS`:

```powershell
uv run python -m scripts.check_live_memory_policy `
  --report artifacts/memory-policy-eval-v2.json
```

One case can be isolated without changing the corpus:

```powershell
uv run python -m scripts.check_live_memory_policy `
  --case user_defined_metric_formula_exact
```

The same gate is available as an explicit pytest integration test:

```powershell
$env:RUN_MEMORY_POLICY_EVAL = "1"
uv run pytest tests/integration/test_memory_policy_live.py `
  -m memory_llm_integration --no-cov
```

`MEMORY_LLM_MAX_TOKENS` defaults to `1000`, matching the runtime baseline. The CLI exits `0` only
when every selected case passes, `1` when a semantic or provider case fails, and `2` for invalid
configuration/report output. It continues after semantic failures to provide a complete matrix,
but stops after the first dependency/protocol failure to avoid repeated timeouts. Reports under
`artifacts/` are intentionally ignored by Git.

## Preserved upstream contract

`tests/vendor/test_mem0_pristine_contract.py` remains the pre-custom compatibility gate: pristine
Mem0 v2.0.20 V3 produces `ADD` or no event, deduplicates only exact user-scoped top-10 matches,
and does not call automatic update/delete. B2 does not modify the vendored engine or impose an
ADD-only adapter contract; well-formed lifecycle outcomes remain provider-owned.

## Deferred work

- A real model run is an environment gate and is not claimed when the internal endpoint is absent.
- Explicit correction and forget remain unautomated.
- `ProcessMemoryUseCase`, persistent formation orchestration, online retrieval, `/chat` context
  integration, cross-session recall, timeouts, and fallback observability remain later batches.
- This corpus is a development acceptance gate, not a production-quality benchmark.

## Local checkpoint evidence — 2026-09-07

- Ruff lint and format checks pass.
- B1/B2 policy, scorer, adapter, and pristine-contract gate: 53 passed; the real-model test was
  skipped because no memory LLM endpoint/model is configured on this host.
- Full repository suite: 313 passed, 17 environment-gated tests skipped; coverage 91.63%.
- PostgreSQL/pgvector integration suite against the isolated synthetic database: 16 passed.
- Internal Mem0 pgvector provider suite: 91 passed.
- Docker image `kira-context:0.3.0` rebuilds successfully and contains the precedence-hardened
  policy v2; the dev-only evaluator and synthetic cases remain outside the runtime image.
- Real Qwen/vLLM semantic gate: **NOT RUN**. B2 is not accepted for deployment until all 18 cases
  pass against the approved internal model deployment.
