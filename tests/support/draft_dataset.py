"""Restore the canonical KiRa dataset to its draft state inside a temp directory."""

import json
import shutil
from hashlib import sha256
from pathlib import Path

from evaluation.dataset import default_dataset_root

_PENDING_ANSWER = "TBD_AFTER_KIRA_FILL"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_draft_dataset(target: Path) -> Path:
    """Copy the canonical dataset and reverse materialization into its draft state.

    The canonical dataset is committed materialized, so tests that exercise the
    pending-materialization workflow must rebuild the draft state: blank the fill-slot
    assistant turns, restore the pending QA answers, and rewind the manifest.
    """
    if target.exists():
        raise FileExistsError(f"draft dataset target already exists: {target}")
    shutil.copytree(default_dataset_root(), target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["bundles"]:
        bundle_root = target / entry["path"]
        files = entry["files"]
        fills = json.loads((bundle_root / files["fills"]["name"]).read_text(encoding="utf-8"))[
            "fills"
        ]
        assistant_ids = {fill["assistant_turn_id"] for fill in fills}
        conversation_path = bundle_root / files["conversation"]["name"]
        conversation = json.loads(conversation_path.read_text(encoding="utf-8"))
        qa_path = bundle_root / files["qa"]["name"]
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        pending = 0
        for turns in conversation["conversation"].values():
            if not isinstance(turns, list):
                continue
            for turn in turns:
                if isinstance(turn, dict) and turn.get("dia_id") in assistant_ids:
                    turn["text"] = ""
        for question in qa["qa"]:
            # expected_api marks exactly the QA rows whose answer comes from KiRa.
            if question.get("expected_api") is None:
                continue
            question["gold_answer"] = None
            question["final_answer"] = _PENDING_ANSWER
            pending += 1
        _write_json(conversation_path, conversation)
        _write_json(qa_path, qa)
        for kind in ("conversation", "qa"):
            descriptor = files[kind]
            descriptor["sha256"] = sha256(
                (bundle_root / descriptor["name"]).read_bytes()
            ).hexdigest()
        entry["materialization_status"] = "pending"
        entry["counts"]["pending_answers"] = pending
    manifest["status"] = "contract_frozen"
    manifest["dataset_version"] = "1.0.0-draft.test"
    _write_json(manifest_path, manifest)
    return target
