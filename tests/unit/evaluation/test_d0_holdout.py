"""d0_holdout_v1 frozen-slice validation tests.

The slice is frozen; these tests fail if the case bytes, gold, count, balance,
theme coverage, provenance pin, or the model-visible payload contract drift.
Cases are loaded from the exact bytes on disk and re-verified against the
manifest pins, so every test runs over the real frozen artifact.
"""

import json
from hashlib import sha256

import pytest
from pydantic import ValidationError

from evaluation.d0_holdout import (
    HOLDOUT_CASE_COUNT,
    HOLDOUT_LABEL_DISTRIBUTION,
    HOLDOUT_ROOT,
    HoldoutCase,
    load_holdout,
    slice_sha256,
)

THEMES = frozenset(
    {
        "current-state-confirmation",
        "definition-vs-usage-rule",
        "role-state-evolution",
        "defaults-preferences",
        "temporary-exception-vs-durable-truth",
    }
)


def _load():
    return load_holdout()


def test_frozen_slice_loads_and_meets_count_balance():
    manifest, cases = _load()
    assert manifest.status == "approved_frozen"
    assert manifest.review.status == "approved_frozen"
    assert len(cases) == HOLDOUT_CASE_COUNT == 15
    distribution = {label: 0 for label in HOLDOUT_LABEL_DISTRIBUTION}
    for case in cases:
        distribution[case.gold_decision] += 1
    assert (
        distribution
        == HOLDOUT_LABEL_DISTRIBUTION
        == {
            "DUPLICATE": 5,
            "KEEP_BOTH": 5,
            "SUPERSEDE": 5,
        }
    )


def test_manifest_pins_match_case_bytes_and_slice_checksum():
    manifest, _cases = _load()
    pinned = {entry["name"]: entry["sha256"] for entry in manifest.files}
    assert len(pinned) == 15
    for name, digest in pinned.items():
        actual = sha256((HOLDOUT_ROOT / name).read_bytes()).hexdigest()
        assert actual == digest, name
    case_hashes = {name: sha256((HOLDOUT_ROOT / name).read_bytes()).hexdigest() for name in pinned}
    assert slice_sha256(case_hashes) == manifest.slice_sha256


def test_every_theme_covers_all_three_labels():
    _manifest, cases = _load()
    by_theme: dict[str, set[str]] = {}
    for case in cases:
        by_theme.setdefault(case.theme, set()).add(case.gold_decision)
    assert set(by_theme) == THEMES
    for theme, labels in by_theme.items():
        assert labels == {"DUPLICATE", "KEEP_BOTH", "SUPERSEDE"}, theme


def test_target_contract_per_decision():
    _manifest, cases = _load()
    for case in cases:
        ids = {row.memory_id for row in case.existing_active_memories}
        if case.gold_decision == "KEEP_BOTH":
            assert case.gold_target_id is None, case.case_id
        else:
            assert case.gold_target_id is not None, case.case_id
            assert case.gold_target_id in ids, case.case_id
    # Exactly one target per target-bearing decision; no multi-target field exists.
    assert not any("gold_target_ids" in case.model_dump() for case in cases)


def test_case_ids_unique_sorted_and_match_filenames():
    _manifest, cases = _load()
    ids = [case.case_id for case in cases]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids) == 15
    for case in cases:
        assert (HOLDOUT_ROOT / "cases" / f"{case.case_id}.json").is_file()


def test_memory_ids_are_scoped_per_case():
    _manifest, cases = _load()
    for case in cases:
        ids = [row.memory_id for row in case.existing_active_memories]
        assert len(set(ids)) == len(ids)
        assert all(row.text.strip() for row in case.existing_active_memories)


def test_no_old_dataset_literals_in_frozen_case_artifact():
    forbidden = (
        "conv01",
        "conv02",
        "conv03",
        "conv04",
        "DTDV",
        "Thượng Long",
        "Thanh Sơn",
        "Chí Tiên",
        "Thượng",
        "bộ lõi",
        "bên mình",
        "5G chính",
        "M01",
        "M05",
        "M08",
        "M09",
        "M10",
        "M13",
        "M14",
        "M15",
        "M17",
        "SổVàng",
    )
    for path in sorted((HOLDOUT_ROOT / "cases").glob("H*.json")):
        text = path.read_text(encoding="utf-8")
        for literal in forbidden:
            assert literal not in text, f"{path.name}: {literal}"


def test_model_visible_payload_contract():
    _manifest, cases = _load()
    for case in cases:
        payload = case.model_visible_payload
        assert set(payload) == {"existing_active_memories", "candidate"}
        assert payload["candidate"] == case.candidate
        assert payload["existing_active_memories"] == [
            {"memory_id": row.memory_id, "text": row.text} for row in case.existing_active_memories
        ]
        # Gold/rationale/theme metadata must not leak as payload keys (memory_ids
        # legitimately appear as values; the gold target is one of them).
        keys = set(payload)
        for forbidden_key in (
            "gold_decision",
            "gold_target_id",
            "rationale",
            "adversarial_property",
            "theme",
        ):
            assert forbidden_key not in keys, f"{case.case_id}: {forbidden_key}"
        for row in payload["existing_active_memories"]:
            assert set(row) == {"memory_id", "text"}


def test_loader_fails_closed_on_byte_drift(tmp_path):
    import shutil

    shutil.copytree(HOLDOUT_ROOT, tmp_path / "slice")
    root = tmp_path / "slice"
    victim = root / "cases" / "H01.json"
    document = json.loads(victim.read_text(encoding="utf-8"))
    document["candidate"] = document["candidate"] + " (drifted)"
    victim.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="drifted from manifest pin"):
        load_holdout(root)


def test_loader_fails_closed_on_gold_drift(tmp_path):
    import shutil

    shutil.copytree(HOLDOUT_ROOT, tmp_path / "slice")
    root = tmp_path / "slice"
    # Rewriting H01's bytes changes its hash; re-pin the manifest to the new bytes
    # so ONLY the gold value drifts, proving gold drift is caught independently.
    victim = root / "cases" / "H01.json"
    document = json.loads(victim.read_text(encoding="utf-8"))
    document["gold_decision"] = "SUPERSEDE"
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    victim.write_bytes(raw)
    manifest_doc = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest_doc["files"]:
        if entry["name"] == "cases/H01.json":
            entry["sha256"] = sha256(raw).hexdigest()
    manifest_doc["slice_sha256"] = slice_sha256(
        {entry["name"]: entry["sha256"] for entry in manifest_doc["files"]}
    )
    (root / "manifest.json").write_bytes(
        (json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    with pytest.raises(ValueError, match="label balance drifted"):
        load_holdout(root)


def test_loader_fails_closed_on_count_or_balance_drift(tmp_path):
    import json as _json
    import shutil

    shutil.copytree(HOLDOUT_ROOT, tmp_path / "slice")
    root = tmp_path / "slice"
    doc = _json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    doc["case_count"] = 14
    (root / "manifest.json").write_text(
        _json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        load_holdout(root)


def test_holdout_case_rejects_multi_target_and_bad_targets():
    base = _load()[1][0].model_dump()
    multi = dict(base)
    multi["gold_target_ids"] = ["t0101", "t0102"]
    with pytest.raises(ValidationError):
        HoldoutCase.model_validate(multi)  # extra="forbid" rejects unknown fields
    missing = dict(base)
    missing["gold_target_id"] = "t9999"
    with pytest.raises(ValidationError):
        HoldoutCase.model_validate(missing)
    keep_with_target = dict(base)
    keep_with_target["gold_decision"] = "KEEP_BOTH"
    with pytest.raises(ValidationError):
        HoldoutCase.model_validate(keep_with_target)


def test_frozen_prompt_pin_provenance():
    manifest, _cases = _load()
    pin = manifest.provenance.frozen_prompt_pin
    assert pin.prompt_version == "d0-conflict-v2"
    assert pin.git_commit == "83d6b71246b9b05944ba2cc45a1e7854119a339f"
    assert pin.prompt_sha256_prefix == "1771c500"
    assert manifest.provenance.prompt_versions_evaluated == (
        "d0-conflict-v1",
        "d0-conflict-v2",
    )
