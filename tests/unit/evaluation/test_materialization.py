"""Materialization planning, resume state and atomic dataset application."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.dataset import default_dataset_root, load_manifest
from evaluation.materialization import (
    MaterializationCheckpoint,
    apply_materialization,
    build_materialization_checkpoint,
    completed_task,
    failed_task,
    load_materialization_checkpoint,
    replace_checkpoint_task,
    verify_checkpoint_source,
    write_materialization_checkpoint,
)
from scripts.validate_dataset import validate_dataset


def _copy_dataset(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    shutil.copytree(default_dataset_root(), root)
    return root


def _complete(checkpoint: MaterializationCheckpoint) -> MaterializationCheckpoint:
    current = checkpoint
    completed_at = datetime(2026, 9, 17, tzinfo=UTC)
    for index, task in enumerate(current.tasks):
        current = replace_checkpoint_task(
            current,
            index,
            completed_task(
                task,
                f"KiRa response {task.task_id[:12]}",
                request_ids=(f"request-{index}",),
                message_ids=(f"message-{index}",),
                now=completed_at,
            ),
            now=completed_at,
        )
    return current


def test_plan_deduplicates_queries_and_covers_every_pending_target():
    checkpoint = build_materialization_checkpoint(
        default_dataset_root(),
        now=datetime(2026, 9, 17, tzinfo=UTC),
    )

    references = [reference for task in checkpoint.tasks for reference in task.references]
    assert checkpoint.total_count == 80
    assert sum(reference.kind == "conversation_fill" for reference in references) == 140
    assert sum(reference.kind == "qa_answer" for reference in references) == 54
    assert len({task.task_id for task in checkpoint.tasks}) == checkpoint.total_count
    assert len({task.query for task in checkpoint.tasks}) == checkpoint.total_count
    assert all(task.status == "pending" for task in checkpoint.tasks)


def test_checkpoint_round_trip_and_terminal_state_guards(tmp_path):
    checkpoint = build_materialization_checkpoint(default_dataset_root())
    task = checkpoint.tasks[0]
    completed = completed_task(
        task,
        "  safe answer  ",
        request_ids=("request-b", "request-a", "request-a"),
    )
    checkpoint = replace_checkpoint_task(checkpoint, 0, completed)
    path = tmp_path / "checkpoint.json"

    write_materialization_checkpoint(path, checkpoint)
    loaded = load_materialization_checkpoint(path)

    assert loaded.tasks[0].response_text == "safe answer"
    assert loaded.tasks[0].request_ids == ("request-a", "request-b")
    assert not path.with_name("checkpoint.json.tmp").exists()
    with pytest.raises(ValueError, match="must not be blank"):
        completed_task(task, "   ")
    failed = failed_task(task, "not-safe-error-class!")
    assert failed.error_class == "MaterializationError"


def test_checkpoint_rejects_source_drift(tmp_path):
    root = _copy_dataset(tmp_path)
    checkpoint = build_materialization_checkpoint(root)
    qa_path = root / "bundles" / "conv04" / "qa.json"
    qa_path.write_text(qa_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source changed"):
        verify_checkpoint_source(root, checkpoint)


def test_apply_requires_complete_checkpoint_and_new_output(tmp_path):
    root = _copy_dataset(tmp_path)
    checkpoint = build_materialization_checkpoint(root)

    with pytest.raises(ValueError, match="incomplete tasks"):
        apply_materialization(
            root,
            checkpoint,
            dataset_version="1.0.0-materialized.1",
            output_root=tmp_path / "output",
        )

    complete = _complete(checkpoint)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        apply_materialization(
            root,
            complete,
            dataset_version="1.0.0-materialized.1",
            output_root=existing,
        )


def test_apply_populates_all_targets_updates_checksums_and_validates(tmp_path):
    root = _copy_dataset(tmp_path)
    checkpoint = _complete(build_materialization_checkpoint(root))
    output = tmp_path / "materialized"

    report = apply_materialization(
        root,
        checkpoint,
        dataset_version="1.0.0-materialized.1",
        output_root=output,
    )

    assert report.unique_queries == 80
    assert report.conversation_fills == 140
    assert report.qa_answers == 54
    manifest = load_manifest(output)
    assert manifest.dataset_version == "1.0.0-materialized.1"
    assert manifest.status == "materialized"
    assert all(bundle.materialization_status == "materialized" for bundle in manifest.bundles)
    assert all(bundle.counts.pending_answers == 0 for bundle in manifest.bundles)
    for entry in manifest.bundles:
        conversation = json.loads(
            (output / entry.path / entry.files.conversation.name).read_text(encoding="utf-8")
        )["conversation"]
        assistant_texts = [
            turn["text"]
            for key, turns in conversation.items()
            if key.startswith("session_") and not key.endswith(("_date_time", "_goal"))
            for turn in turns
            if turn["role"] == "assistant"
        ]
        assert all(assistant_texts)
        questions = json.loads(
            (output / entry.path / entry.files.qa.name).read_text(encoding="utf-8")
        )["qa"]
        assert all(question.get("final_answer") != "TBD_AFTER_KIRA_FILL" for question in questions)
    assert validate_dataset(output).valid
    assert validate_dataset(root).valid


def test_apply_in_place_uses_validated_staging_copy(tmp_path):
    root = _copy_dataset(tmp_path)
    checkpoint = _complete(build_materialization_checkpoint(root))

    report = apply_materialization(
        root,
        checkpoint,
        dataset_version="1.0.0-materialized.1",
    )

    assert Path(report.output_root) == root.resolve()
    assert load_manifest(root).status == "materialized"
    assert validate_dataset(root).valid
