# Week 5 offline benchmark handoff

This handoff moves exact runtime/controller images from a connected build machine to the internal
PC. It does not move credentials, does not install Python packages at container startup, and does
not claim model quality from the mock acceptance.

## Fixed paths and images

- Historical control source: `75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00`.
- Materialization checkpoint: `artifacts/week5/kira-materialization.json`.
- Benchmark artifacts: `artifacts/week5/benchmark/`.
- Handoff bundle: `artifacts/week5/offline-handoff/`.
- Required images: control Gateway/Worker, candidate Gateway/Worker, eval controller and pgvector.

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

`Build` creates detached worktrees for the exact control/candidate SHAs, builds with `--pull=false`,
checks OCI revision/contract/role labels and writes `image-manifest.json`. `MockAcceptance` runs with
`--network none`; it validates/compiles the canonical corpus and runs all four dependency preflights
with deterministic doubles. It is plumbing evidence only. `Export` writes one image tar plus SHA-256
and copies the secret-free Compose/env templates into the bundle.

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

Populate `.env.week5.internal.local` locally. Never commit or put it into the image tar. Replace its
three image references with the exact references in `image-manifest.json`; fill the exact internal
model/deployment names, embedding dimension, KiRa endpoint and credentials. Use the same provider
revisions and retrieval config for control and candidate.

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
  -m scripts.materialize_dataset collect `
  --checkpoint /materialization/kira-materialization.json --resume
docker compose --env-file .env.week5.internal.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.materialize_dataset apply --checkpoint /materialization/kira-materialization.json `
  --dataset-version <reviewed-version> --in-place
```

Do not apply an incomplete checkpoint. Review/freeze the resulting gold revision before the official
benchmark.

## 4. Sequential startup to limit peak RAM

Run one variant at a time. The script starts PostgreSQL, then migrations, memory schema init, Worker
and Gateway; it never starts control and candidate together:

```powershell
./scripts/week5_offline_handoff.ps1 -Action StartControl
# run control and preserve artifacts, then:
./scripts/week5_offline_handoff.ps1 -Action Stop
./scripts/week5_offline_handoff.ps1 -Action StartCandidate
# run candidate and preserve artifacts, then:
./scripts/week5_offline_handoff.ps1 -Action Stop
```

`Stop` intentionally retains named database volumes. Use only the benchmark cleanup manifest or an
explicitly approved Compose volume removal after evidence has been copied; never run broad deletes
against an application database.

## Acceptance checklist

- The image tar SHA-256 and every loaded Docker image ID match `image-manifest.json`.
- OCI `revision`, benchmark `contract`, `variant` and `role` labels match the manifest.
- Offline mock acceptance passes with Docker network disabled.
- No populated env file, bearer/basic credential or database password is in Git/image archive.
- Internal preflight passes before any paid/official run.
- Control and candidate use the same corpus revision, providers, embedding space and run config.
- Every official output lands under the artifact root; materialization uses the fixed checkpoint.
- Missing traces do not change quality outcomes; Langfuse/OTel remain optional debug evidence.
