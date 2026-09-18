# Week 5 offline benchmark handoff

This handoff moves exact runtime/controller images from a connected build machine to the internal
PC. It does not move credentials, does not install Python packages at container startup, and does
not claim model quality from the mock acceptance.

## Fixed paths and images

- Historical control source: `75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00`.
- Materialization checkpoint: `artifacts/week5/kira-materialization.json`.
- Benchmark artifacts: `artifacts/week5/benchmark/`.
- Handoff bundle: `artifacts/week5/offline-handoff/`.
- Exact benchmark image set: one runtime image and one eval image for the control, plus one runtime
  image and one eval image for every declared candidate. One candidate therefore produces four
  benchmark images; two candidates produce six. The pinned pgvector image is a separate runtime
  dependency and is not counted as a benchmark variant image.

The runtime and eval images contain their locked dependencies. `pull_policy: never` is used by the
internal Compose file. No service runs `pip`, `uv sync`, model download or package installation at
startup. Model, embedding, judge and KiRa endpoints remain external internal-network dependencies.

## 1. Build on the connected laptop

Commit the candidate first and make sure the checkout is clean. Ensure the pinned pgvector image is
already present, then run:

```powershell
docker pull pgvector/pgvector:0.8.6-pg16-bookworm
./scripts/week5_offline_handoff.ps1 -Action Build -CandidateRevision HEAD
./scripts/week5_offline_handoff.ps1 -Action MockAcceptance
./scripts/week5_offline_handoff.ps1 -Action Export
```

To declare two candidates, pass both committed revisions in order. They become `candidate-a` and
`candidate-b`:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Build `
  -CandidateRevision @("<candidate-a-sha>", "<candidate-b-sha>")
```

`Build` creates detached worktrees for the exact runtime SHAs. The eval code always comes from the
clean harness revision that launches the build, but each eval image is bound to exactly one runtime
revision by OCI labels. The script builds with `--pull=false`, checks source revision, runtime
revision, contract, variant and role labels, then writes schema-v2 `image-manifest.json` with the
declared variants. `MockAcceptance` runs the control eval image with `--network none`; it
validates/compiles the canonical corpus and runs all four dependency preflights with deterministic
doubles. It is plumbing evidence only. `Export` writes one image tar plus SHA-256 and copies the
secret-free Compose/env templates into the bundle.

An internal registry can replace the tar transfer:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Publish -Registry registry.internal.example
```

Keep the generated manifest beside registry release metadata; do not replace immutable revisions
with `latest`.

## 2. Transfer and verify on the company PC

Transfer the repository and `artifacts/week5/offline-handoff/`. Verify/load without Internet:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Import
Copy-Item evaluation/week5.internal.env.example .env.week5.internal.local
```

Populate `.env.week5.internal.local` locally. Never commit or put it into the image tar. The three
image values in this file are placeholders used only for Compose validation; `StartControl` and
`StartCandidate` select the exact runtime/eval pair from `image-manifest.json` and override them for
that invocation. Fill the exact internal model/deployment names, embedding dimension, KiRa endpoint
and credentials. Use the same provider revisions and retrieval config for control and candidates.

Validate Compose without printing resolved configuration:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Validate
```

## 3. Materialize only the missing KiRa fields

The checkpoint is resume-safe and rewritten after every completed request. Run materialization in
the locked eval image so the company PC does not need to install Python packages. The host dataset
is mounted read-write and the checkpoint is mounted under `/artifacts`:

```powershell
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.materialize_dataset plan
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.materialize_dataset preflight `
  --checkpoint /materialization/kira-materialization.json
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.materialize_dataset collect `
  --checkpoint /materialization/kira-materialization.json --resume
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.materialize_dataset apply --checkpoint /materialization/kira-materialization.json `
  --dataset-version <reviewed-version> --in-place
```

Do not apply an incomplete checkpoint. Review/freeze the resulting gold revision before the official
benchmark. `preflight` makes exactly one real KiRa request and stores it in the same official
checkpoint. Re-running preflight reuses that completed task; the following `collect --resume` skips
it instead of spending a second request.

Export a hash-bound review packet from the materialized copy. Review all KiRa answers, memory
lifecycle events, rewrite constraints, task/API expectations, safety cases and semantic
near-duplicates; then replace every placeholder in the decisions file. Freezing fails unless every
bundle has the same reviewer/revision, every checklist item is true and the source files still have
the exact packet hashes:

```powershell
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.review_dataset export `
  --packet /materialization/dataset-review-packet.json `
  --decisions /materialization/dataset-review-decisions.json
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.review_dataset freeze `
  --packet /materialization/dataset-review-packet.json `
  --decisions /materialization/dataset-review-decisions.json `
  --dataset-version 1.0.0 --allow-pc-openai --in-place
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm eval-controller
```

`--allow-pc-openai` is an explicit data-governance approval for this reviewed synthetic-derived
corpus. It does not make the PC run official; only `internal_test` artifacts can claim official
benchmark evidence.

## 4. Sequential startup to limit peak RAM

Run one variant at a time. The script starts PostgreSQL, then migrations, memory schema init, Worker
and Gateway; it never starts control and candidate together:

```powershell
./scripts/week5_offline_handoff.ps1 -Action StartControl
# run control and preserve artifacts, then:
./scripts/week5_offline_handoff.ps1 -Action Stop
./scripts/week5_offline_handoff.ps1 -Action StartCandidate -VariantId candidate-a
# run candidate and preserve artifacts, then:
./scripts/week5_offline_handoff.ps1 -Action Stop
```

If the manifest declares a second candidate, repeat with `-VariantId candidate-b`. The script rejects
undeclared candidates and never silently reuses another candidate's eval/runtime image pair.

`Stop` intentionally retains named database volumes. Use only the benchmark cleanup manifest or an
explicitly approved Compose volume removal after evidence has been copied; never run broad deletes
against an application database.

## Acceptance checklist

- The image tar SHA-256 and every loaded Docker image ID match `image-manifest.json`.
- OCI source `revision`, `runtime-revision`, benchmark `contract`, `variant` and `role` labels match
  the manifest.
- Offline mock acceptance passes with Docker network disabled.
- No populated env file, bearer/basic credential or database password is in Git/image archive.
- Internal preflight passes before any paid/official run.
- Control and candidate use the same corpus revision, providers, embedding space and run config.
- Every official output lands under the artifact root; materialization uses the fixed checkpoint.
- Missing traces do not change quality outcomes; Langfuse/OTel remain optional debug evidence.
