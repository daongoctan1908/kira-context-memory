# D0 Conflict Holdout Slice (d0_holdout_v1)

Adversarial out-of-sample slice for registered D0 conflict prompts. This directory is
separate from `kira_ltm_v1` (which remains the frozen full-corpus dataset); per that
dataset's README, holdout evaluation uses a separately authored corpus rather than
relabeling part of the full corpus.

- 15 cases: 5 DUPLICATE / 5 KEEP_BOTH / 5 SUPERSEDE, 5 themes × 3-label triplet
- Each theme's triplet shares vocabulary/entities so lexical overlap alone cannot solve the task
- Case text is Vietnamese, matching the production assistant surface

## Model-visible payload contract

An evaluation run may hand the LLM only `existing_active_memories` and `candidate`
(`evaluation.d0_holdout.HoldoutCase.model_visible_payload` is the only sanctioned
builder). `gold_decision`, `gold_target_id`, `rationale`, and `adversarial_property`
exist for offline adjudication only and must never reach the model.

## Frozen bytes

`manifest.json` pins the SHA-256 of every `cases/H*.json` file plus `slice_sha256`,
a checksum over the sorted (name, sha256) pairs of the exact case bytes. The loader
(`evaluation/d0_holdout.load_holdout`) fails closed on any byte, gold, count,
balance, theme-coverage, or checksum drift.

After freeze, cases and gold must not be edited based on model outputs; any change
requires a new slice version and a fresh human review.
