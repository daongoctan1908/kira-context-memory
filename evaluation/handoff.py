"""Fail-closed evidence binding for the offline VDI/Kubernetes handoff."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from evaluation.compiler import compile_dataset
from evaluation.dataset import load_manifest
from evaluation.models import BENCHMARK_CONTRACT_ID, EvalModel, Identifier, Sha256
from evaluation.pc_acceptance import PcAcceptanceManifest
from evaluation.pc_preflight import PcPreflightFreeze


class HandoffEvidence(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    created_at: datetime
    official: Literal[False] = False
    scope: Literal["pc_technical_acceptance_handoff"] = "pc_technical_acceptance_handoff"
    dataset_id: Identifier
    dataset_version: str = Field(min_length=1, max_length=64)
    dataset_sha256: Sha256
    dataset_manifest_sha256: Sha256
    review_reviewer: Identifier
    review_revision: Identifier
    image_manifest_sha256: Sha256
    pc_preflight_sha256: Sha256
    pc_acceptance_sha256: Sha256
    variant_ids: tuple[Identifier, ...] = Field(min_length=2, max_length=3)
    image_count: int = Field(ge=5, le=7, strict=True)


def file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def build_handoff_evidence(
    *,
    dataset_root: Path,
    image_manifest_path: Path,
    pc_preflight_path: Path,
    pc_acceptance_path: Path,
    now: datetime | None = None,
) -> HandoffEvidence:
    """Verify the immutable PC evidence before any image archive is exported."""

    dataset = load_manifest(dataset_root)
    if dataset.status != "benchmark_ready" or not dataset.data_policy.external_provider_allowed:
        raise ValueError("handoff requires the reviewed PC-approved dataset")
    reviews = {(bundle.review.reviewer, bundle.review.revision) for bundle in dataset.bundles}
    if len(reviews) != 1 or None in next(iter(reviews)):
        raise ValueError("handoff requires one complete dataset review identity")
    reviewer, review_revision = next(iter(reviews))
    assert reviewer is not None and review_revision is not None

    compilation = compile_dataset(dataset_root, seed=742)
    acceptance = PcAcceptanceManifest.model_validate_json(
        pc_acceptance_path.read_text(encoding="utf-8")
    )
    if not acceptance.technical_passed or acceptance.official:
        raise ValueError("PC technical acceptance has not passed")
    if acceptance.dataset_sha256 != compilation.dataset_sha256:
        raise ValueError("PC acceptance is bound to another dataset")

    preflight = PcPreflightFreeze.model_validate_json(pc_preflight_path.read_text(encoding="utf-8"))
    if not preflight.ready or preflight.dataset_sha256 != compilation.dataset_sha256:
        raise ValueError("PC preflight is not ready for this dataset")
    if acceptance.config_sha256 != preflight.config_sha256:
        raise ValueError("PC acceptance and preflight configs differ")

    image_manifest_sha256 = file_sha256(image_manifest_path)
    pc_preflight_sha256 = file_sha256(pc_preflight_path)
    if acceptance.image_manifest_sha256 != image_manifest_sha256:
        raise ValueError("image manifest changed after PC acceptance")
    if acceptance.pc_preflight_sha256 != pc_preflight_sha256:
        raise ValueError("PC preflight changed after acceptance")

    image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
    if image_manifest.get("contract_id") != BENCHMARK_CONTRACT_ID:
        raise ValueError("image manifest uses another benchmark contract")
    variants = image_manifest.get("variants")
    images = image_manifest.get("images")
    if not isinstance(variants, list) or not isinstance(images, list):
        raise ValueError("invalid image manifest")
    variant_ids = tuple(item.variant_id for item in acceptance.variants)
    declared_ids = tuple(item.get("variant_id") for item in variants if isinstance(item, dict))
    if declared_ids != variant_ids:
        raise ValueError("accepted variants differ from handoff images")
    expected_image_count = 1 + 2 * len(variant_ids)
    if len(images) != expected_image_count:
        raise ValueError("handoff image count does not match declared variants")
    if (
        sum(isinstance(item, dict) and item.get("role") == "postgres-dependency" for item in images)
        != 1
    ):
        raise ValueError("handoff requires exactly one pinned PostgreSQL dependency image")

    return HandoffEvidence(
        created_at=now or datetime.now(UTC),
        dataset_id=compilation.dataset_id,
        dataset_version=compilation.dataset_version,
        dataset_sha256=compilation.dataset_sha256,
        dataset_manifest_sha256=file_sha256(dataset_root / "manifest.json"),
        review_reviewer=reviewer,
        review_revision=review_revision,
        image_manifest_sha256=image_manifest_sha256,
        pc_preflight_sha256=pc_preflight_sha256,
        pc_acceptance_sha256=file_sha256(pc_acceptance_path),
        variant_ids=variant_ids,
        image_count=len(images),
    )
