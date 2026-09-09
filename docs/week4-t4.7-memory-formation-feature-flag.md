# Week 4 T4.7 — Memory formation feature flag

## Decision

`MEMORY_FORMATION_ENABLED` controls only whether a completed conversation turn is eligible to
schedule an asynchronous memory job. It is independent from `LTM_ENABLED`, which controls online
long-term-memory retrieval in the Gateway.

Both flags default to `false`. This keeps a newly deployed Gateway on the Week 3 behavior until an
operator deliberately enables each capability.

| `LTM_ENABLED` | `MEMORY_FORMATION_ENABLED` | Gateway behavior |
| --- | --- | --- |
| `false` | `false` | No LTM retrieval and no formation scheduling. |
| `false` | `true` | Schedule eligible completed turns; do not construct Mem0 in the Gateway. |
| `true` | `false` | Retrieve LTM online; do not schedule new formation jobs. |
| `true` | `true` | Retrieve LTM online and schedule eligible completed turns. |

## Configuration contract

- Environment values are parsed as booleans; ambiguous values fail settings validation.
- `LTM_ENABLED=true` still requires the complete Mem0 database, embedding, and memory-LLM
  configuration because the Gateway constructs a retrieval adapter.
- `MEMORY_FORMATION_ENABLED=true` does not require Mem0 provider configuration. The Gateway only
  requests an atomic PostgreSQL job insert; the future Worker owns Mem0 formation dependencies.
- The settings object is immutable after startup. Changing either environment variable requires a
  process restart; there is no per-request override.
- Neither flag changes minimum readiness. Startup/wiring errors still fail according to the owning
  component's existing contract; runtime contextual degradation does not synthesize an answer.

## Scope boundary

T4.7 defines and verifies the independent flag contract. Completion-callback eligibility,
scheduling outcome metrics, and the full disabled/error regression matrix belong to T4.8–T4.10.
No Worker loop or synchronous Mem0 formation is introduced here.

## Acceptance evidence

Automated tests verify:

1. The default for both flags is off.
2. All four retrieval/formation combinations are representable.
3. Formation-only mode does not require Mem0 runtime configuration.
4. Formation-only Gateway startup does not construct `Mem0Adapter`.
5. Invalid `MEMORY_FORMATION_ENABLED` text fails configuration validation.

Run the focused gate:

```powershell
uv run pytest tests/unit/config/test_settings.py `
  tests/integration/test_gateway_api.py -k "formation or safe_baseline" --no-cov
```

The full Ruff, formatting, and pytest gates remain mandatory before the T4.7 commit.
