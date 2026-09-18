"""Human review packet binding and atomic dataset freeze tests."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.dataset import default_dataset_root, load_manifest
from evaluation.materialization import (
    apply_materialization,
    build_materialization_checkpoint,
    completed_task,
    replace_checkpoint_task,
)
from evaluation.review import (
    BundleReviewDecision,
    ReviewChecklist,
    build_review_packet,
    freeze_reviewed_dataset,
)
from scripts.validate_dataset import validate_dataset


def _materialized_dataset(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    shutil.copytree(default_dataset_root(), source)
    checkpoint = build_materialization_checkpoint(source)
    for index, task in enumerate(checkpoint.tasks):
        checkpoint = replace_checkpoint_task(
            checkpoint,
            index,
            completed_task(
                task,
                f"KiRa materialized answer {index}",
                now=datetime(2026, 9, 18, tzinfo=UTC),
            ),
            now=datetime(2026, 9, 18, tzinfo=UTC),
        )
    apply_materialization(
        source,
        checkpoint,
        dataset_version="1.0.0-materialized.1",
    )
    return source


def _approvals(root: Path) -> tuple[BundleReviewDecision, ...]:
    source_sha256 = build_review_packet(root).source_sha256
    return tuple(
        BundleReviewDecision(
            bundle_id=bundle.bundle_id,
            source_sha256=source_sha256,
            verdict="approved",
            reviewer="mentor",
            revision="gold-v1",
            checklist=ReviewChecklist(
                materialized_kira_answers=True,
                memory_lifecycle=True,
                rewrite_constraints=True,
                expected_task_api=True,
                safety_cases=True,
                semantic_near_duplicates=True,
            ),
            notes="Reviewed against conversation evidence and the benchmark rubric.",
        )
        for bundle in load_manifest(root).bundles
    )


def test_review_packet_requires_materialized_source():
    with pytest.raises(ValueError, match="materialized"):
        build_review_packet(default_dataset_root())


def test_review_freeze_is_hash_bound_complete_and_enables_pc_acceptance(tmp_path):
    source = _materialized_dataset(tmp_path)
    packet = build_review_packet(source)
    output = tmp_path / "reviewed"

    assert sum(item.materialized_final_answers for item in packet.bundles) == 54

    report = freeze_reviewed_dataset(
        source,
        packet=packet,
        decisions=_approvals(source),
        dataset_version="1.0.0",
        allow_pc_openai=True,
        output_root=output,
    )

    manifest = load_manifest(output)
    assert report.source_sha256 == packet.source_sha256
    assert manifest.status == "benchmark_ready"
    assert manifest.dataset_version == "1.0.0"
    assert manifest.data_policy.external_provider_allowed is True
    assert all(bundle.review.status == "reviewed" for bundle in manifest.bundles)
    assert all(bundle.review.reviewer == "mentor" for bundle in manifest.bundles)
    assert validate_dataset(output).valid
    assert load_manifest(source).status == "materialized"


def test_review_freeze_rejects_incomplete_or_stale_approval(tmp_path):
    source = _materialized_dataset(tmp_path)
    packet = build_review_packet(source)
    approvals = _approvals(source)
    with pytest.raises(ValueError, match="every bundle"):
        freeze_reviewed_dataset(
            source,
            packet=packet,
            decisions=approvals[:-1],
            dataset_version="1.0.0",
            allow_pc_openai=True,
            output_root=tmp_path / "incomplete",
        )

    qa_path = source / "bundles" / "conv04" / "qa.json"
    qa_path.write_text(qa_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        freeze_reviewed_dataset(
            source,
            packet=packet,
            decisions=approvals,
            dataset_version="1.0.0",
            allow_pc_openai=True,
            output_root=tmp_path / "stale",
        )


def test_approved_decision_requires_every_manual_check():
    with pytest.raises(ValueError, match="every review check"):
        BundleReviewDecision(
            bundle_id="conv01",
            source_sha256="a" * 64,
            verdict="approved",
            reviewer="mentor",
            revision="gold-v1",
            checklist=ReviewChecklist(materialized_kira_answers=True),
            notes="Incomplete review must not freeze the dataset.",
        )
