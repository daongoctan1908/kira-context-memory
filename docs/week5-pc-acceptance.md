# Week 5 T4.5 — Company-PC full acceptance

The company-PC run is a **non-official technical acceptance**, not a candidate promotion gate.
OpenAI metrics are diagnostic because the official target uses internal models in Kubernetes.

## Preconditions

- the canonical dataset is materialized, reviewed, `benchmark_ready`, and explicitly approved for
  the PC external provider;
- T4.3 produced one exact runtime/eval image pair for the control and each declared candidate;
- T4.4 produced a ready `pc-preflight/freeze.json` bound to the same dataset and config;
- every variant starts with a fresh, isolated benchmark database and runs sequentially;
- control and candidates use the same corpus, case order, seed `742`, providers, retrieval config,
  timeout policy, and scorer revision.

Never reuse a completed run directory. Resume only the exact run identity already recorded in its
`manifest.json`; the case ledger skips terminal cases and cannot silently change provenance.

## Run matrix

Run the four suites once for `control`, then once for each declared candidate (at most two):

```text
formation -> retrieval -> rewrite -> cross_session
```

Use `pc_openai_acceptance`, persistent formation, the exact provenance file emitted for that image,
and a separate artifact directory under:

```text
artifacts/week5/pc-openai/<run-set-id>/<variant>/
```

The full runner must finish before technical acceptance is checked. A dependency/protocol result is
not converted into a quality failure and remains unresolved evidence. A blocked dataset case must
have a terminal `NOT_RUN`; every eligible case must have one of `PASS`, `FAIL`,
`REVIEW_REQUIRED`, or `INSUFFICIENT_EVIDENCE`.

## Check the technical handoff gate

After all variant runs have stopped and their artifacts have been copied out of the containers:

```powershell
python -m scripts.check_pc_acceptance `
  --run control=artifacts/week5/pc-openai/<run-set-id>/control `
  --run candidate-a=artifacts/week5/pc-openai/<run-set-id>/candidate-a `
  --dataset-root dataset/kira_ltm_v1 `
  --image-manifest artifacts/week5/offline-handoff/image-manifest.json `
  --pc-preflight artifacts/week5/pc-preflight/freeze.json `
  --output artifacts/week5/pc-openai/<run-set-id>/pc-acceptance.json
```

Add `--run candidate-b=...` only when it is declared in the image manifest. The command is
create-only and exits nonzero unless all technical gates pass.

The checker binds the evidence to:

- exact dataset ID/version/hash baked into every eval image;
- exact runtime and harness revisions;
- exact prompt hashes and package versions;
- exact T4.4 config hash;
- identical ordered full corpus for every variant;
- non-official PC profile and all four suites.

## The only hard gates

1. Every eligible case has a quality-terminal artifact.
2. No dependency, protocol, missing, or unexpected `NOT_RUN` result remains.
3. No nested `safety_violation_codes` value exists.
4. Dataset compilation, artifact identity, and image/preflight provenance all match.

Formation Precision/Recall/F1, Retrieval Recall@3/MRR@10, rewrite judgments, final-QA judgments,
task success, timing, retries, and control/candidate differences must still be reported. They are
diagnostics only on the PC. `UNCERTAIN`, a semantic failure, or lower OpenAI quality does not by
itself promote or reject a candidate.

## Failure routing

- Dependency/protocol/harness failure: fix the runtime or harness and rerun the affected exact run.
- Gold/dataset defect: return to T4.2, bump dataset version, commit, rebuild every eval image, rerun
  T4.4 and all T4.5 variants.
- Safety hard fail: fix before creating the handoff bundle.
- Non-blocking quality weakness: record it; optionally declare a new candidate revision, but do not
  label it promoted from PC evidence.

The laptop validates schemas, fail-closed binding, and checker behavior. Real KiRa, OpenAI,
PostgreSQL/Gateway/Worker execution and the resulting acceptance manifest remain `NOT_RUN` until the
company PC is available.
