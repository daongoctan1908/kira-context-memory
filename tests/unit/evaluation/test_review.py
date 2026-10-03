"""Human review packet binding and atomic dataset freeze tests."""

import json
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
    update_external_provider_policy,
)
from scripts.benchmark.validate_dataset import validate_dataset
from tests.support.draft_dataset import copy_draft_dataset


def _materialized_dataset(tmp_path: Path) -> Path:
    source = copy_draft_dataset(tmp_path / "source")
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


def test_review_packet_requires_materialized_source(tmp_path):
    with pytest.raises(ValueError, match="materialized"):
        build_review_packet(copy_draft_dataset(tmp_path / "source"))


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


def test_policy_authorization_preserves_payload_and_existing_review(tmp_path):
    root = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), root)
    before = json.loads((root / "manifest.json").read_bytes())
    payloads = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "bundles").rglob("*.json")
    }
    audit = tmp_path / "policy-audit.json"
    report = update_external_provider_policy(
        root,
        dataset_version="1.0.0-pc-authorized.1",
        allow_pc_openai=True,
        actor="dataset-owner",
        authorization_reference="user-message-2026-10-03",
        notes="Owner explicitly authorizes this frozen corpus for OpenAI PC benchmark.",
        audit_path=audit,
    )
    after = json.loads((root / "manifest.json").read_bytes())
    assert len(report.payload_files) == 16
    assert all((root / relative).read_bytes() == content for relative, content in payloads.items())
    assert before["bundles"] == after["bundles"]
    assert after["data_policy"]["external_provider_allowed"]
    assert report.before_manifest_sha256 != report.after_manifest_sha256
    assert json.loads(audit.read_text(encoding="utf-8"))["actor"] == "dataset-owner"
    assert validate_dataset(root).valid


@pytest.mark.parametrize("failure", ["stale", "unchanged", "invalid_actor", "audit_exists"])
def test_policy_authorization_fails_closed_before_dataset_write(tmp_path, failure):
    root = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), root)
    before = (root / "manifest.json").read_bytes()
    audit = tmp_path / "policy-audit.json"
    if failure == "stale":
        target = next((root / "bundles").rglob("*.json"))
        target.write_bytes(target.read_bytes() + b"\n")
    if failure == "audit_exists":
        audit.write_text("existing evidence", encoding="utf-8")
    with pytest.raises((ValueError, FileExistsError)):
        update_external_provider_policy(
            root,
            dataset_version="1.0.0-pc-authorized.1",
            allow_pc_openai=failure != "unchanged",
            actor="" if failure == "invalid_actor" else "dataset-owner",
            authorization_reference="user-message-2026-10-03",
            notes="Explicit owner authorization.",
            audit_path=audit,
        )
    assert (root / "manifest.json").read_bytes() == before


def test_policy_authorization_copy_and_revocation_do_not_replace_human_review(tmp_path):
    source = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), source)
    before = (source / "manifest.json").read_bytes()
    output = tmp_path / "approved"
    kwargs = {
        "actor": "dataset-owner",
        "authorization_reference": "owner-policy-update",
        "notes": "Explicit policy change, content review remains valid.",
    }
    update_external_provider_policy(
        source,
        dataset_version="pc-authorized.1",
        allow_pc_openai=True,
        audit_path=tmp_path / "allow.json",
        output_root=output,
        **kwargs,
    )
    assert (source / "manifest.json").read_bytes() == before
    update_external_provider_policy(
        output,
        dataset_version="pc-revoked.1",
        allow_pc_openai=False,
        audit_path=tmp_path / "revoke.json",
        **kwargs,
    )
    assert not load_manifest(output).data_policy.external_provider_allowed
    assert load_manifest(output).bundles == load_manifest(source).bundles
