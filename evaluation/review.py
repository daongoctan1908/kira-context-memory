"""Hash-bound human review and freeze workflow for the materialized canonical dataset."""

from __future__ import annotations

import json
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from evaluation.dataset import DatasetManifest, load_manifest
from evaluation.models import EvalModel, Identifier, NonEmpty, Sha256


class BundleReviewItem(EvalModel):
    bundle_id: Identifier
    revision: int = Field(ge=1, strict=True)
    conversation_fills: int = Field(ge=0, strict=True)
    memory_events: int = Field(ge=0, strict=True)
    materialized_final_answers: int = Field(ge=0, strict=True)
    hard_gate_cases: int = Field(ge=0, strict=True)
    diagnostic_cases: int = Field(ge=0, strict=True)
    files: dict[Identifier, Sha256]


class DatasetReviewPacket(EvalModel):
    schema_version: Literal[1] = 1
    dataset_id: Identifier
    dataset_version: NonEmpty
    source_sha256: Sha256
    required_checks: tuple[Identifier, ...] = (
        "materialized_kira_answers",
        "memory_lifecycle",
        "rewrite_constraints",
        "expected_task_api",
        "safety_cases",
        "semantic_near_duplicates",
    )
    bundles: tuple[BundleReviewItem, ...] = Field(min_length=1)


class ReviewChecklist(EvalModel):
    materialized_kira_answers: bool = False
    memory_lifecycle: bool = False
    rewrite_constraints: bool = False
    expected_task_api: bool = False
    safety_cases: bool = False
    semantic_near_duplicates: bool = False

    @property
    def complete(self) -> bool:
        return all(self.model_dump().values())


class BundleReviewDecision(EvalModel):
    bundle_id: Identifier
    source_sha256: Sha256
    verdict: Literal["approved", "rejected"]
    reviewer: Identifier
    revision: Identifier
    checklist: ReviewChecklist
    notes: NonEmpty

    @model_validator(mode="after")
    def approval_requires_complete_checklist(self) -> BundleReviewDecision:
        if self.verdict == "approved" and not self.checklist.complete:
            raise ValueError("approved bundle requires every review check")
        return self


class DatasetFreezeReport(EvalModel):
    dataset_id: Identifier
    dataset_version: NonEmpty
    source_sha256: Sha256
    bundle_count: int = Field(ge=1, strict=True)
    reviewer: Identifier
    review_revision: Identifier
    external_provider_allowed: bool
    output_root: NonEmpty


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _source_payload(root: Path, manifest: DatasetManifest) -> dict[str, object]:
    bundles: list[dict[str, object]] = []
    for bundle in manifest.bundles:
        files: dict[str, str] = {}
        for kind, descriptor in bundle.files.items():
            path = root / bundle.path / descriptor.name
            actual = _digest(path)
            if actual != descriptor.sha256:
                raise ValueError(f"dataset checksum mismatch: {bundle.bundle_id}/{kind}")
            files[kind] = actual
        bundles.append(
            {
                "bundle_id": bundle.bundle_id,
                "revision": bundle.revision,
                "files": files,
            }
        )
    return {
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "bundles": bundles,
    }


def dataset_review_source_sha256(root: Path, manifest: DatasetManifest) -> str:
    payload = json.dumps(
        _source_payload(root.resolve(), manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def build_review_packet(root: Path) -> DatasetReviewPacket:
    dataset_root = root.resolve()
    manifest = load_manifest(dataset_root)
    if manifest.status != "materialized":
        raise ValueError("review export requires a materialized dataset")
    if any(
        bundle.materialization_status != "materialized" or bundle.counts.pending_answers
        for bundle in manifest.bundles
    ):
        raise ValueError("review export requires every materialization target")
    payload = _source_payload(dataset_root, manifest)

    def materialized_final_answers(bundle_index: int) -> int:
        bundle = manifest.bundles[bundle_index]
        document = json.loads(
            (dataset_root / bundle.path / bundle.files.qa.name).read_text(encoding="utf-8")
        )
        rows = document.get("qa") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            raise ValueError("QA document has no reviewable rows")
        return sum(
            isinstance(row, dict)
            and isinstance(row.get("gold_rewrite"), str)
            and bool(row["gold_rewrite"].strip())
            for row in rows
        )

    items = tuple(
        BundleReviewItem(
            bundle_id=bundle.bundle_id,
            revision=bundle.revision,
            conversation_fills=bundle.counts.fills,
            memory_events=bundle.counts.memory_events,
            materialized_final_answers=materialized_final_answers(index),
            hard_gate_cases=bundle.counts.hard_gate,
            diagnostic_cases=bundle.counts.diagnostic_history,
            files=payload["bundles"][index]["files"],  # type: ignore[index]
        )
        for index, bundle in enumerate(manifest.bundles)
    )
    return DatasetReviewPacket(
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.dataset_version,
        source_sha256=dataset_review_source_sha256(dataset_root, manifest),
        bundles=items,
    )


def decision_template(packet: DatasetReviewPacket) -> list[dict[str, object]]:
    return [
        {
            "bundle_id": bundle.bundle_id,
            "source_sha256": packet.source_sha256,
            "verdict": "rejected",
            "reviewer": "replace_me",
            "revision": "replace_me",
            "checklist": {name: False for name in packet.required_checks},
            "notes": "Replace with a concrete review note.",
        }
        for bundle in packet.bundles
    ]


def freeze_reviewed_dataset(
    root: Path,
    *,
    packet: DatasetReviewPacket,
    decisions: tuple[BundleReviewDecision, ...],
    dataset_version: str,
    allow_pc_openai: bool,
    output_root: Path | None = None,
) -> DatasetFreezeReport:
    source_root = root.resolve()
    manifest = load_manifest(source_root)
    if packet != build_review_packet(source_root):
        raise ValueError("review packet differs from the materialized dataset")
    if not dataset_version.strip() or dataset_version == manifest.dataset_version:
        raise ValueError("freeze requires a new nonblank dataset version")
    by_bundle = {decision.bundle_id: decision for decision in decisions}
    expected = {bundle.bundle_id for bundle in manifest.bundles}
    if len(by_bundle) != len(decisions) or set(by_bundle) != expected:
        raise ValueError("review decisions must cover every bundle exactly once")
    if any(decision.source_sha256 != packet.source_sha256 for decision in decisions):
        raise ValueError("review decision is not bound to this materialized dataset")
    if any(decision.verdict != "approved" for decision in decisions):
        raise ValueError("every bundle must be approved before freeze")
    reviewers = {decision.reviewer for decision in decisions}
    revisions = {decision.revision for decision in decisions}
    if len(reviewers) != 1 or len(revisions) != 1:
        raise ValueError("dataset freeze requires one reviewer and one review revision")
    reviewer = next(iter(reviewers))
    review_revision = next(iter(revisions))
    destination = output_root.resolve() if output_root is not None else source_root
    if output_root is not None and destination.exists():
        raise FileExistsError("reviewed output directory already exists")

    with tempfile.TemporaryDirectory(prefix="kira-review-", dir=source_root.parent) as raw:
        staged_root = Path(raw) / source_root.name
        shutil.copytree(source_root, staged_root)
        manifest_path = staged_root / "manifest.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["dataset_version"] = dataset_version.strip()
        document["status"] = "benchmark_ready"
        document["data_policy"]["external_provider_allowed"] = allow_pc_openai
        for bundle in document["bundles"]:
            decision = by_bundle[bundle["bundle_id"]]
            bundle["review"] = {
                "status": "reviewed",
                "reviewer": decision.reviewer,
                "revision": decision.revision,
            }
        manifest_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        from scripts.validate_dataset import validate_dataset

        validation = validate_dataset(staged_root)
        if not validation.valid:
            raise ValueError("reviewed dataset failed deterministic validation")
        if output_root is None:
            temporary = source_root / "manifest.json.reviewing"
            temporary.write_bytes(manifest_path.read_bytes())
            temporary.replace(source_root / "manifest.json")
        else:
            shutil.copytree(staged_root, destination)

    return DatasetFreezeReport(
        dataset_id=manifest.dataset_id,
        dataset_version=dataset_version.strip(),
        source_sha256=packet.source_sha256,
        bundle_count=len(decisions),
        reviewer=reviewer,
        review_revision=review_revision,
        external_provider_allowed=allow_pc_openai,
        output_root=str(destination),
    )
