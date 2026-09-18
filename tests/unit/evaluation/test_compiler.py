"""Deterministic compilation and source-coverage contracts."""

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.compiler import compilation_json_bytes, compile_dataset
from evaluation.dataset import default_dataset_root, iter_messages, load_bundle, load_manifest
from evaluation.materialization import (
    apply_materialization,
    build_materialization_checkpoint,
    completed_task,
    replace_checkpoint_task,
)
from evaluation.models import FormationInput, Suite


def _copy_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), target)
    return target


def _replace_file_checksum(root: Path, bundle_index: int, kind: str) -> None:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["bundles"][bundle_index]
    descriptor = entry["files"][kind]
    contents = (root / entry["path"] / descriptor["name"]).read_bytes()
    descriptor["sha256"] = hashlib.sha256(contents).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_compiler_emits_all_suites_namespaces_and_source_coverage():
    compilation = compile_dataset(seed=23)

    assert len(compilation.cases) == 534
    assert {
        suite.suite: (suite.total, suite.eligible, suite.blocked)
        for suite in compilation.coverage.suites
    } == {
        Suite.FORMATION: (62, 62, 0),
        Suite.RETRIEVAL: (209, 209, 0),
        Suite.REWRITE: (54, 54, 0),
        Suite.CROSS_SESSION: (209, 155, 54),
    }
    assert {
        source.kind: (source.total, source.linked, source.accounted_not_selected)
        for source in compilation.coverage.sources
    } == {
        "conversation": (506, 130, 376),
        "fill": (140, 0, 140),
        "memory": (62, 62, 0),
        "qa": (209, 209, 0),
    }
    assert len(compilation.coverage.rows) == 506 + 140 + 62 + 209
    assert all(":" in case.case_id for case in compilation.cases)
    assert all(":" in row.row_id for row in compilation.coverage.rows)
    coverage_ids = {row.row_id for row in compilation.coverage.rows}
    assert all(set(case.source_row_ids).issubset(coverage_ids) for case in compilation.cases)

    formation = next(case for case in compilation.cases if case.suite is Suite.FORMATION)
    assert isinstance(formation.inputs, FormationInput)
    assert formation.gold.lifecycle_event is not None
    assert all(message.timestamp is not None for message in formation.inputs.messages)
    assert all(
        message.timestamp is not None and message.timestamp.utcoffset() is not None
        for message in formation.inputs.messages
    )


def test_compiler_is_byte_deterministic_for_dataset_and_seed():
    first = compile_dataset(seed=742)
    second = compile_dataset(seed=742)
    different_seed = compile_dataset(seed=743)

    assert compilation_json_bytes(first) == compilation_json_bytes(second)
    assert first.dataset_sha256 == second.dataset_sha256 == different_seed.dataset_sha256
    assert {case.case_id for case in first.cases} == {case.case_id for case in different_seed.cases}
    assert [case.case_id for case in first.cases[:20]] != [
        case.case_id for case in different_seed.cases[:20]
    ]
    with pytest.raises(ValueError, match="seed must be an integer"):
        compile_dataset(seed=True)


def test_pending_kira_answers_are_blocked_without_silent_case_loss():
    compilation = compile_dataset()
    blocked = [case for case in compilation.cases if case.eligibility.status == "blocked"]

    assert len(blocked) == 54
    assert all(case.suite is Suite.CROSS_SESSION for case in blocked)
    assert all(
        case.eligibility.blocked_reasons == ("pending_kira_final_answer",) for case in blocked
    )
    assert [(reason.reason, reason.cases) for reason in compilation.coverage.blocked_reasons] == [
        ("pending_kira_final_answer", 54)
    ]


def test_pending_evidence_assistant_blocks_formation_case(tmp_path):
    root = _copy_dataset(tmp_path)
    conversation_path = root / "bundles" / "conv01" / "conversation.json"
    document = json.loads(conversation_path.read_text(encoding="utf-8"))
    assistant = next(
        turn for turn in document["conversation"]["session_1"] if turn["dia_id"] == "D1:8"
    )
    assistant["text"] = ""
    conversation_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _replace_file_checksum(root, 0, "conversation")

    compilation = compile_dataset(root)
    case = next(item for item in compilation.cases if item.case_id == "conv01:formation:M01")

    assert case.eligibility.status == "blocked"
    assert case.eligibility.blocked_reasons == ("pending_kira_assistant",)
    assert all(message.message_id != "conv01:D1:8" for message in case.inputs.messages)


def test_materialized_dataset_unblocks_cross_session_cases(tmp_path):
    root = _copy_dataset(tmp_path)
    checkpoint = build_materialization_checkpoint(root, now=datetime(2026, 9, 18, tzinfo=UTC))
    for index, task in enumerate(checkpoint.tasks):
        checkpoint = replace_checkpoint_task(
            checkpoint,
            index,
            completed_task(
                task,
                f"Materialized KiRa answer {index}",
                now=datetime(2026, 9, 18, tzinfo=UTC),
            ),
            now=datetime(2026, 9, 18, tzinfo=UTC),
        )
    output = tmp_path / "materialized"
    apply_materialization(
        root,
        checkpoint,
        dataset_version="1.0.0-materialized.compiler-test",
        output_root=output,
    )

    compilation = compile_dataset(output)

    cross = next(item for item in compilation.coverage.suites if item.suite is Suite.CROSS_SESSION)
    assert (cross.total, cross.eligible, cross.blocked) == (209, 209, 0)
    assert compilation.coverage.blocked_reasons == ()


def test_formation_and_cross_session_ingestion_uses_only_conversation_rows():
    root = default_dataset_root()
    manifest = load_manifest(root)
    conversation_messages = {}
    for entry in manifest.bundles:
        bundle = load_bundle(entry, root=root)
        conversation_messages.update(
            (message.message_id, (message.content, message.timestamp))
            for message in iter_messages(bundle)
            if message.content
        )

    compilation = compile_dataset(root)
    for case in compilation.cases:
        if case.suite is Suite.FORMATION:
            messages = case.inputs.messages
        elif case.suite is Suite.CROSS_SESSION:
            messages = case.inputs.session_a_messages
        else:
            continue
        for message in messages:
            assert (message.content, message.timestamp) == conversation_messages[message.message_id]


def test_compiler_fails_closed_on_checksum_drift(tmp_path):
    root = _copy_dataset(tmp_path)
    qa_path = root / "bundles" / "conv04" / "qa.json"
    qa_path.write_text(qa_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="dataset checksum mismatch"):
        compile_dataset(root)


def test_compiler_rejects_duplicate_generated_case_ids(tmp_path):
    root = _copy_dataset(tmp_path)
    qa_path = root / "bundles" / "conv01" / "qa.json"
    document = json.loads(qa_path.read_text(encoding="utf-8"))
    document["qa"].append(document["qa"][0])
    qa_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _replace_file_checksum(root, 0, "qa")

    with pytest.raises(ValueError, match="duplicate compiled case ID"):
        compile_dataset(root)


def test_compiler_rejects_unknown_evidence_reference(tmp_path):
    root = _copy_dataset(tmp_path)
    memories_path = root / "bundles" / "conv01" / "memories.json"
    document = json.loads(memories_path.read_text(encoding="utf-8"))
    document["memory_gold"][0]["source_turn_ids"] = ["D404:1"]
    memories_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _replace_file_checksum(root, 0, "memories")

    with pytest.raises(ValueError, match="unknown evidence message: conv01:D404:1"):
        compile_dataset(root)
