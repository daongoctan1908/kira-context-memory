"""Typed, fail-closed access to the D0 conflict holdout slice (d0_holdout_v1).

The holdout is a separately authored adversarial slice for out-of-sample evaluation
of registered D0 conflict prompts; it is NOT part of the kira_ltm_v1 full-corpus
dataset and never replaces it. Case bytes are frozen: the manifest pins the SHA-256
of every case file plus a whole-slice checksum over the sorted (name, sha256) pairs,
and loading fails closed on any byte or metadata drift.

Model-visible payload contract: an evaluation run may hand the LLM only
``existing_active_memories`` and ``candidate``. Gold and review metadata exist for
offline adjudication only and are excluded from the payload builder below.
"""

import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from evaluation.models import EvalModel, NonEmpty, Sha256

HOLDOUT_SLICE_ID = "d0_holdout_v1"
HOLDOUT_ROOT = Path(__file__).resolve().parents[1] / "dataset" / HOLDOUT_SLICE_ID
HOLDOUT_CASE_COUNT = 15
HOLDOUT_LABEL_DISTRIBUTION = {"DUPLICATE": 5, "KEEP_BOTH": 5, "SUPERSEDE": 5}

MemoryId = Annotated[str, StringConstraints(pattern=r"^t[0-9]{4}$")]
CaseId = Annotated[str, StringConstraints(pattern=r"^H(0[1-9]|1[0-5])$")]


class HoldoutMemoryRow(EvalModel):
    memory_id: MemoryId
    text: NonEmpty


class HoldoutCase(EvalModel):
    """One frozen conflict case; gold/rationale fields are adjudication-only."""

    schema_version: Literal[1] = 1
    case_id: CaseId
    theme: Literal[
        "current-state-confirmation",
        "definition-vs-usage-rule",
        "role-state-evolution",
        "defaults-preferences",
        "temporary-exception-vs-durable-truth",
    ]
    existing_active_memories: tuple[HoldoutMemoryRow, ...] = Field(min_length=2, max_length=5)
    candidate: NonEmpty
    gold_decision: Literal["DUPLICATE", "KEEP_BOTH", "SUPERSEDE"]
    gold_target_id: MemoryId | None = None
    rationale: NonEmpty
    adversarial_property: NonEmpty

    @model_validator(mode="after")
    def target_rules(self) -> "HoldoutCase":
        memory_ids = {row.memory_id for row in self.existing_active_memories}
        if len(memory_ids) != len(self.existing_active_memories):
            raise ValueError(f"{self.case_id}: duplicate memory_id in existing rows")
        if self.gold_decision == "KEEP_BOTH":
            if self.gold_target_id is not None:
                raise ValueError(f"{self.case_id}: KEEP_BOTH must have a null target")
        elif self.gold_target_id is None or self.gold_target_id not in memory_ids:
            raise ValueError(
                f"{self.case_id}: {self.gold_decision} requires exactly one existing target"
            )
        return self

    @property
    def model_visible_payload(self) -> dict[str, object]:
        """The ONLY fields an evaluation run may hand the LLM."""
        return {
            "existing_active_memories": [
                {"memory_id": row.memory_id, "text": row.text}
                for row in self.existing_active_memories
            ],
            "candidate": self.candidate,
        }


class HoldoutProvenancePin(EvalModel):
    prompt_version: str
    git_commit: str
    prompt_sha256_prefix: str


class HoldoutAuthoring(EvalModel):
    author: str
    review_protocol: str


class HoldoutProvenance(EvalModel):
    prompt_versions_evaluated: tuple[str, ...] = Field(min_length=1)
    frozen_prompt_pin: HoldoutProvenancePin
    authoring: HoldoutAuthoring


class HoldoutReview(EvalModel):
    status: Literal["draft", "approved_frozen"]
    reviewer: str | None = None
    revision: str | None = None

    @model_validator(mode="after")
    def approved_has_provenance(self) -> "HoldoutReview":
        if self.status == "approved_frozen" and (not self.reviewer or not self.revision):
            raise ValueError("approved_frozen holdout requires reviewer and revision")
        return self


class HoldoutManifest(EvalModel):
    schema_alias: str = Field(alias="$schema")
    schema_version: Literal[1]
    slice_id: Literal["d0_holdout_v1"]
    slice_version: str
    status: Literal["draft", "approved_frozen"]
    canonical_format: Literal["json"]
    evaluation_scope: Literal["holdout_slice"]
    case_count: int
    label_distribution: dict[str, int]
    review: HoldoutReview
    provenance: HoldoutProvenance
    files: tuple[Annotated[dict[str, str], Field(min_length=1)], ...]
    slice_sha256: Sha256

    @model_validator(mode="after")
    def frozen_shape(self) -> "HoldoutManifest":
        if self.status == "approved_frozen":
            if self.review.status != "approved_frozen":
                raise ValueError("frozen holdout requires an approved_frozen review block")
            if self.case_count != HOLDOUT_CASE_COUNT:
                raise ValueError(f"frozen holdout must contain exactly {HOLDOUT_CASE_COUNT} cases")
            if self.label_distribution != HOLDOUT_LABEL_DISTRIBUTION:
                raise ValueError(
                    "frozen holdout must hold the 5 DUPLICATE / 5 KEEP_BOTH / 5 SUPERSEDE balance"
                )
        return self


def _case_bytes(case_id: str) -> bytes:
    path = HOLDOUT_ROOT / "cases" / f"{case_id}.json"
    if not path.is_file():
        raise ValueError(f"holdout case file missing: {path.name}")
    return path.read_bytes()


def slice_sha256(case_hashes: dict[str, str]) -> str:
    """Whole-slice checksum over sorted (name, sha256) pairs of exact case bytes."""
    payload = json.dumps(
        sorted(case_hashes.items()), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def load_holdout_manifest(root: Path = HOLDOUT_ROOT) -> HoldoutManifest:
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return HoldoutManifest.model_validate(document)


def load_holdout(root: Path = HOLDOUT_ROOT) -> tuple[HoldoutManifest, tuple[HoldoutCase, ...]]:
    """Load the frozen slice, verifying every pinned case hash and the slice
    checksum over exact bytes; any drift fails closed."""
    manifest = load_holdout_manifest(root)
    pinned = {entry["name"]: entry["sha256"] for entry in manifest.files}
    expected_names = {f"cases/{case_id}.json" for case_id in _CASE_IDS}
    if set(pinned) != expected_names:
        raise ValueError("holdout manifest must pin exactly the 15 canonical case files")

    cases: list[HoldoutCase] = []
    hashes: dict[str, str] = {}
    for name in sorted(pinned):
        raw = (root / name).read_bytes()
        digest = sha256(raw).hexdigest()
        if digest != pinned[name]:
            raise ValueError(f"holdout case bytes drifted from manifest pin: {name}")
        hashes[name] = digest
        case = HoldoutCase.model_validate_json(raw)
        expected_case_id = Path(name).stem
        if case.case_id != expected_case_id:
            raise ValueError(f"case_id does not match filename: {name}")
        cases.append(case)

    if manifest.case_count != len(cases):
        raise ValueError("manifest case_count does not match observed case files")
    distribution: dict[str, int] = {"DUPLICATE": 0, "KEEP_BOTH": 0, "SUPERSEDE": 0}
    for case in cases:
        distribution[case.gold_decision] += 1
    if distribution != HOLDOUT_LABEL_DISTRIBUTION:
        raise ValueError(f"holdout label balance drifted: {distribution}")
    if manifest.label_distribution != distribution:
        raise ValueError("manifest label_distribution does not match case gold")

    case_ids = [case.case_id for case in cases]
    if case_ids != sorted(case_ids) or len(set(case_ids)) != len(case_ids):
        raise ValueError("holdout case ids must be unique and sorted")

    if slice_sha256(hashes) != manifest.slice_sha256:
        raise ValueError("holdout slice checksum drifted from manifest pin")

    themes: dict[str, set[str]] = {}
    for case in cases:
        themes.setdefault(case.theme, set()).add(case.gold_decision)
    expected_labels = {"DUPLICATE", "KEEP_BOTH", "SUPERSEDE"}
    for theme, labels in themes.items():
        if labels != expected_labels:
            raise ValueError(f"theme {theme!r} does not cover all three labels: {sorted(labels)}")

    return manifest, tuple(cases)


_CASE_IDS = tuple(f"H{index:02d}" for index in range(1, HOLDOUT_CASE_COUNT + 1))
