# KiRa Long-Term Memory Dataset v1

This directory is the canonical, versioned source dataset for the KiRa memory benchmark. It is
synthetic data derived from internal KiRa testcase contracts; it must not be sent to an external
provider or published outside the approved repository without a separate data review.

## Current status

- Dataset status: `contract_frozen`
- Materialization status: `pending`
- Bundles: 4
- Sessions: 86
- Turns: 506
- KiRa fill slots: 140
- Gold memory events: 62
- QA rows: 209
- Pending KiRa-backed answers: 54

`contract_frozen` means that the conversations, fill mappings, memory lifecycle gold and QA gold
have been frozen. It does **not** mean that the dataset is ready for a final benchmark run. The
blank assistant turns must first be materialized using the pinned KiRa system, and the remaining
`TBD_AFTER_KIRA_FILL` answers must be populated from those responses.

## Layout

```text
kira_ltm_v1/
├── manifest.json
├── schemas/
└── bundles/
    ├── conv01/
    ├── conv02/
    ├── conv03/
    └── conv04/
```

Each bundle contains four canonical files:

- `conversation.json`: the ordered multi-session synthetic conversation;
- `fills.json`: KiRa query and expected API contracts for blank assistant turns;
- `memories.json`: memory-event lifecycle gold; this is never an ingestion corpus;
- `qa.json`: QA, retrieval, rewrite and scoring gold; this is never an ingestion corpus.

CSV, review prose and audit reports are generated views and are intentionally not stored beside
the canonical JSON.

## IDs and timestamps

IDs such as `M01` and `D1:1` are local to a bundle. Code must namespace them as
`<bundle_id>:<local_id>`, for example `conv04:M01`.

The source records one timezone-aware timestamp per session. The loader derives deterministic
per-message timestamps by adding the zero-based turn index in microseconds. This preserves source
ordering without rewriting the synthetic source transcript.

## Evaluation scope

This is a single `full_corpus` acceptance dataset. Every official run evaluates all four bundles
and all eligible cases together. `scenario_group` and `memory_family` remain reporting/audit
dimensions only; they do not assign development or holdout subsets.

Because the same full corpus can be inspected while prompts are developed, results must not be
presented as unseen-holdout generalization evidence. A future tuning experiment that requires an
independent holdout must use a separately collected corpus rather than hiding part of this one.

## Validation

Run the deterministic validator before any benchmark or commit:

```powershell
uv run python -m scripts.validate_dataset dataset/kira_ltm_v1
```

The validator checks the manifest and checksums, full-corpus scope, counts, IDs, references, fill
pairing, lifecycle links, pending/materialized state and normalized exact duplicates.

The manifest deliberately keeps gold review status as `draft` until a named reviewer approves a
specific revision. A schema pass is not human review.
