# Week 5 T4.4 — Company-PC OpenAI + KiRa preflight

T4.4 freezes technical dependency evidence for the **non-official** PC acceptance run. It does not
promote a candidate and does not replace the later internal-model benchmark.

## Inputs

- canonical dataset is `benchmark_ready`, reviewed and explicitly approved for the PC external
  provider;
- T4.1 materialization checkpoint is complete (`80/80`) and remains outside Git;
- exact T4.3 image manifest exists;
- `.env.week5.internal.local` contains explicit extraction, rewrite, embedding and judge endpoints,
  model/deployment names and credentials; shared `OPENAI_*` values are never inherited;
- one control/candidate stack is already running and healthy.

The provider preflight now includes extraction JSON, rewrite, embedding dimension, semantic judge
JSON schema, PostgreSQL/pgvector/schema, Gateway and Worker. Real KiRa evidence comes from the T4.1
materialization checkpoint; the old `/_test/requests` identity check is mock-only and is never used
as evidence for the PC profile.

## Run the sanitized provider preflight

Select the running variant in `.env.week5.internal.local`. For control use:

```ini
WEEK5_BENCHMARK_GATEWAY_HOST=control-gateway
WEEK5_BENCHMARK_WORKER_HOST=control-worker
WEEK5_BENCHMARK_POSTGRES_HOST=control-postgres
WEEK5_BENCHMARK_DATABASE=kira_control
```

For the candidate use the four `candidate-*` defaults from the template. Then run:

```powershell
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm eval-controller preflight `
  --profile pc_openai_acceptance `
  --suite formation --suite retrieval --suite rewrite --suite cross_session `
  --formation-mode persistent `
  --provenance-file /materialization/<exact-variant-provenance>.json `
  --output /artifacts/pc-preflight/provider-preflight.json
```

The output is create-only: rerunning cannot overwrite evidence or spend requests accidentally.
Provider calls have zero automatic retry. Raw prompts, responses, DSNs, API keys and exception text
are absent from the artifact.

## Bind provider evidence to real KiRa and the reviewed dataset

```powershell
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.freeze_pc_preflight `
  --provider-preflight /artifacts/pc-preflight/provider-preflight.json `
  --materialization-checkpoint /materialization/kira-materialization.json `
  --dataset-root /app/dataset/kira_ltm_v1 `
  --output /artifacts/pc-preflight/freeze.json
```

Freeze succeeds only when all four suites have successful required probes, judge is configured,
provenance is clean, the real KiRa checkpoint is complete, and the dataset is reviewed,
benchmark-ready and external-approved. The freeze stores hashes and sanitized identities only; it
does not copy KiRa response text or credentials.

## Laptop status versus PC status

On the laptop, schema, masking, failure semantics and deterministic mock requests are tested. The
following remain `NOT_RUN` until the company PC is available:

- real KiRa auth/SSE and complete materialization;
- real OpenAI model/embedding/judge calls;
- real pgvector/Gateway/Worker readiness for each exact image pair;
- creation of `provider-preflight.json` and `freeze.json` from those live dependencies.
