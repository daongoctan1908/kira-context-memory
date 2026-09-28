"""Dataset packaging, namespace and deterministic validation contracts."""

import json
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evaluation.dataset import (
    DatasetFile,
    default_dataset_root,
    iter_messages,
    load_bundle,
    load_manifest,
    namespace_id,
)
from scripts.benchmark.validate_dataset import main, validate_dataset
from tests.support.draft_dataset import copy_draft_dataset


def test_canonical_dataset_loads_with_namespaced_deterministic_messages():
    root = default_dataset_root()
    manifest = load_manifest(root)

    assert manifest.dataset_id == "kira_ltm_v1"
    assert manifest.evaluation_scope == "full_corpus"
    assert tuple(bundle.bundle_id for bundle in manifest.bundles) == (
        "conv01",
        "conv02",
        "conv03",
        "conv04",
    )
    with pytest.raises(KeyError):
        manifest.bundle("missing")

    entry = manifest.bundle("conv04")
    bundle = load_bundle(entry, root=root)
    messages = list(iter_messages(bundle))

    assert len(messages) == entry.counts.turns == 102
    assert messages[0].message_id == "conv04:D1:1"
    assert messages[0].session_id == "conv04:session-01"
    assert messages[0].local_message_id == "D1:1"
    assert messages[1].timestamp - messages[0].timestamp == timedelta(microseconds=1)
    assert messages[0].timestamp.utcoffset() is not None
    assert sum(not message.content for message in messages) == (
        entry.counts.fills if entry.materialization_status == "pending" else 0
    )


def test_dataset_path_and_identifier_guards():
    assert default_dataset_root().name == "kira_ltm_v1"
    assert namespace_id("conv01", "M01") == "conv01:M01"
    with pytest.raises(ValueError):
        namespace_id("", "M01")
    with pytest.raises(ValueError):
        namespace_id("conv01", "")
    with pytest.raises(ValidationError):
        DatasetFile(name="../qa.json", sha256="0" * 64)


def test_message_loader_rejects_invalid_source_shapes():
    root = default_dataset_root()
    manifest = load_manifest(root)
    bundle = load_bundle(manifest.bundle("conv01"), root=root)

    invalid = replace(bundle, conversation={"conversation": []})
    with pytest.raises(ValueError, match="object named conversation"):
        list(iter_messages(invalid))

    conversation = json.loads(json.dumps(bundle.conversation))
    conversation["conversation"]["session_1_date_time"] = "2026-09-10T08:35:00"
    invalid = replace(bundle, conversation=conversation)
    with pytest.raises(ValueError, match="timezone-aware"):
        list(iter_messages(invalid))

    conversation = json.loads(json.dumps(bundle.conversation))
    conversation["conversation"]["session_1"][0]["role"] = "system"
    invalid = replace(bundle, conversation=conversation)
    with pytest.raises(ValueError, match="unsupported role"):
        list(iter_messages(invalid))

    conversation = json.loads(json.dumps(bundle.conversation))
    conversation["conversation"]["session_1"] = {}
    invalid = replace(bundle, conversation=conversation)
    with pytest.raises(ValueError, match="must be an array"):
        list(iter_messages(invalid))


def _copy_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), target)
    return target


def test_validator_accepts_canonical_dataset_and_cli_formats(capsys):
    report = validate_dataset(default_dataset_root())
    assert report.valid
    assert report.errors == ()
    assert report.counts == {
        "diagnostic_history": 74,
        "fills": 140,
        "hard_gate": 135,
        "memory_events": 62,
        "pending_answers": 0,
        "qa": 209,
        "sessions": 86,
        "turns": 506,
    }
    assert main([str(default_dataset_root())]) == 0
    assert capsys.readouterr().out.startswith("PASS dataset=kira_ltm_v1")
    assert main([str(default_dataset_root()), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
    assert output["counts"]["qa"] == 209


def test_validator_rejects_checksum_drift(tmp_path):
    root = _copy_dataset(tmp_path)
    qa_path = root / "bundles" / "conv04" / "qa.json"
    qa_path.write_text(qa_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    report = validate_dataset(root)

    assert not report.valid
    assert "conv04/qa.json checksum mismatch" in report.errors


def test_validator_enforces_materialization_and_manifest_contract(tmp_path):
    root = copy_draft_dataset(tmp_path / "dataset")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["bundles"][0]
    entry["materialization_status"] = "materialized"
    entry["counts"]["pending_answers"] = 0
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report = validate_dataset(root)

    assert not report.valid
    assert "conv01 is materialized but still has blank assistant turns" in report.errors
    assert "conv01 is materialized but still has pending answers" in report.errors

    manifest["bundles"][1]["bundle_id"] = "conv01"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = validate_dataset(root)
    assert report.dataset_id is None
    assert report.errors == ("invalid manifest: ValidationError",)


def test_load_bundle_requires_json_objects(tmp_path):
    root = _copy_dataset(tmp_path)
    manifest = load_manifest(root)
    entry = manifest.bundle("conv01")
    path = root / entry.path / entry.files.qa.name
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a JSON object"):
        load_bundle(entry, root=root)
