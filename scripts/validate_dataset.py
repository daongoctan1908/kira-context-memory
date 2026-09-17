"""Validate the canonical KiRa LTM dataset and its frozen cross-file contracts."""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evaluation.dataset import DatasetManifest, load_bundle, load_manifest

_SESSION_KEY = re.compile(r"^session_([1-9][0-9]*)$")
_SECRET = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~-]{12,}|sk-[a-z0-9_-]{12,}|"
    r"(?:api[_-]?key|password|passwd)\s*[:=]\s*[\"'][^\"']+[\"'])"
)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    dataset_id: str | None
    dataset_version: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    counts: dict[str, int]

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_json(self) -> str:
        return json.dumps(
            {"valid": self.valid, **asdict(self)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _object_list(value: Any, label: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        errors.append(f"{label} must be an array of objects")
        return []
    return value


def _unique_ids(
    records: list[dict[str, Any]],
    field: str,
    label: str,
    errors: list[str],
) -> set[str]:
    values: list[str] = []
    for record in records:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"{label} contains a missing or invalid {field}")
            continue
        values.append(value)
    duplicates = sorted(key for key, count in Counter(values).items() if count > 1)
    if duplicates:
        errors.append(f"{label} has duplicate {field}: {duplicates}")
    return set(values)


def _references(
    values: Any,
    valid: set[str],
    label: str,
    errors: list[str],
) -> None:
    if values is None:
        return
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        errors.append(f"{label} must be an array of IDs")
        return
    missing = sorted(set(values) - valid)
    if missing:
        errors.append(f"{label} references missing IDs: {missing}")


def _validate_supersession(
    records: list[dict[str, Any]],
    memory_ids: set[str],
    label: str,
    errors: list[str],
) -> None:
    edges: dict[str, str] = {}
    for record in records:
        memory_id = record.get("memory_id")
        supersedes = record.get("supersedes_memory_id")
        if supersedes is not None:
            if supersedes not in memory_ids:
                errors.append(f"{label}/{memory_id} supersedes missing memory {supersedes}")
            elif memory_id == supersedes:
                errors.append(f"{label}/{memory_id} cannot supersede itself")
            else:
                edges[str(memory_id)] = str(supersedes)
    for start in edges:
        seen: set[str] = set()
        current = start
        while current in edges:
            if current in seen:
                errors.append(f"{label} has a supersession cycle containing {current}")
                break
            seen.add(current)
            current = edges[current]


def _validate_bundle(
    root: Path,
    manifest: DatasetManifest,
    bundle_index: int,
    errors: list[str],
    warnings: list[str],
    global_fill_ids: set[str],
    normalized_questions: dict[str, list[str]],
    normalized_facts: dict[str, list[str]],
) -> dict[str, int]:
    entry = manifest.bundles[bundle_index]
    label = entry.bundle_id
    bundle_root = (root / entry.path).resolve()
    if root != bundle_root and root not in bundle_root.parents:
        errors.append(f"{label} escapes the dataset root")
        return {}
    for _, descriptor in entry.files.items():
        path = bundle_root / descriptor.name
        if not path.is_file():
            errors.append(f"{label}/{descriptor.name} is missing")
        elif _digest(path) != descriptor.sha256:
            errors.append(f"{label}/{descriptor.name} checksum mismatch")
    try:
        bundle = load_bundle(entry, root=root)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        errors.append(f"cannot load {label}: {error.__class__.__name__}")
        return {}

    documents = (bundle.conversation, bundle.fills, bundle.memories, bundle.qa)
    conversation_ids = {doc.get("conversation_id") for doc in documents}
    if conversation_ids != {entry.source_conversation_id}:
        errors.append(f"{label} has inconsistent conversation_id values: {conversation_ids}")
    serialized = json.dumps(documents, ensure_ascii=False)
    if _SECRET.search(serialized):
        errors.append(f"{label} contains a credential-like value")

    conversation = bundle.conversation.get("conversation")
    metadata = bundle.conversation.get("metadata")
    if not isinstance(conversation, dict) or not isinstance(metadata, dict):
        errors.append(f"{label}/conversation.json is missing conversation or metadata")
        return {}
    session_pairs = sorted(
        (int(match.group(1)), key) for key in conversation if (match := _SESSION_KEY.fullmatch(key))
    )
    expected_numbers = list(range(1, len(session_pairs) + 1))
    if [number for number, _ in session_pairs] != expected_numbers:
        errors.append(f"{label} session numbers must be contiguous from 1")
    turns: list[dict[str, Any]] = []
    position: dict[str, tuple[str, int, dict[str, Any]]] = {}
    actual_turn_counts: list[int] = []
    for _, session_key in session_pairs:
        date_key = f"{session_key}_date_time"
        goal_key = f"{session_key}_goal"
        try:
            source_time = datetime.fromisoformat(str(conversation[date_key]))
        except (KeyError, ValueError):
            errors.append(f"{label}/{session_key} has an invalid date_time")
            source_time = None
        if source_time is not None and (
            source_time.tzinfo is None or source_time.utcoffset() is None
        ):
            errors.append(f"{label}/{session_key} timestamp must be timezone-aware")
        if not isinstance(conversation.get(goal_key), str) or not conversation[goal_key].strip():
            errors.append(f"{label}/{session_key} has no goal")
        session_turns = _object_list(
            conversation.get(session_key), f"{label}/{session_key}", errors
        )
        actual_turn_counts.append(len(session_turns))
        for index, turn in enumerate(session_turns):
            expected_role = "user" if index % 2 == 0 else "assistant"
            if turn.get("role") != expected_role:
                errors.append(f"{label}/{session_key}[{index}] must be {expected_role}")
            dia_id = turn.get("dia_id")
            if not isinstance(dia_id, str) or not dia_id:
                errors.append(f"{label}/{session_key}[{index}] has no dia_id")
            elif dia_id in position:
                errors.append(f"{label} has duplicate dia_id {dia_id}")
            else:
                position[dia_id] = (session_key, index, turn)
            if not isinstance(turn.get("text"), str):
                errors.append(f"{label}/{session_key}[{index}] text must be a string")
        turns.extend(session_turns)
    if metadata.get("sessions") != len(session_pairs):
        errors.append(f"{label} metadata.sessions mismatch")
    if metadata.get("turns") != len(turns):
        errors.append(f"{label} metadata.turns mismatch")
    if metadata.get("session_turn_counts") != actual_turn_counts:
        errors.append(f"{label} metadata.session_turn_counts mismatch")

    fills = _object_list(bundle.fills.get("fills"), f"{label}/fills", errors)
    fill_ids = _unique_ids(fills, "fill_id", f"{label}/fills", errors)
    overlap = sorted(fill_ids & global_fill_ids)
    if overlap:
        errors.append(f"fill IDs are not globally unique: {overlap}")
    global_fill_ids.update(fill_ids)
    fill_assistant_ids: set[str] = set()
    for fill in fills:
        fill_id = fill.get("fill_id", "<unknown>")
        user_id = fill.get("user_turn_id")
        assistant_id = fill.get("assistant_turn_id")
        if user_id not in position or assistant_id not in position:
            errors.append(f"{label}/{fill_id} references a missing turn")
            continue
        user_session, user_index, user_turn = position[user_id]
        assistant_session, assistant_index, assistant_turn = position[assistant_id]
        if user_session != assistant_session or assistant_index != user_index + 1:
            errors.append(f"{label}/{fill_id} is not an adjacent user/assistant pair")
        if user_turn.get("role") != "user" or assistant_turn.get("role") != "assistant":
            errors.append(f"{label}/{fill_id} has invalid turn roles")
        if fill.get("surface_text") != user_turn.get("text"):
            errors.append(f"{label}/{fill_id} surface_text differs from the source turn")
        if fill.get("session_datetime") != conversation.get(f"{user_session}_date_time"):
            errors.append(f"{label}/{fill_id} session_datetime mismatch")
        contract = fill.get("runner_contract")
        if not isinstance(contract, dict) or contract.get("send_field") not in fill:
            errors.append(f"{label}/{fill_id} has an invalid runner_contract")
        if isinstance(assistant_id, str):
            fill_assistant_ids.add(assistant_id)
    blank_assistant_ids = {
        str(turn.get("dia_id"))
        for turn in turns
        if turn.get("role") == "assistant" and turn.get("text") == ""
    }
    if blank_assistant_ids != fill_assistant_ids:
        errors.append(f"{label} blank assistant turns do not match fill slots")
    if metadata.get("kira_fill_slots") != len(fills):
        errors.append(f"{label} metadata.kira_fill_slots mismatch")

    memories = _object_list(bundle.memories.get("memory_gold"), f"{label}/memory_gold", errors)
    memory_ids = _unique_ids(memories, "memory_id", f"{label}/memory_gold", errors)
    if bundle.memories.get("do_not_ingest") is not True:
        errors.append(f"{label}/memories.json must set do_not_ingest=true")
    for memory in memories:
        memory_id = str(memory.get("memory_id", "<unknown>"))
        for field in ("source_turn_ids", "primary_source_turn_ids", "supporting_turn_ids"):
            _references(memory.get(field), set(position), f"{label}/{memory_id}.{field}", errors)
        fact = memory.get("canonical_fact")
        if isinstance(fact, str) and fact.strip():
            normalized_facts[_normalize(fact)].append(f"{label}:{memory_id}")
        else:
            errors.append(f"{label}/{memory_id} has a blank canonical_fact")
    _validate_supersession(memories, memory_ids, label, errors)
    active_declared = bundle.memories.get("active_memory_event_ids_at_end")
    active_computed = {
        str(memory.get("memory_id")) for memory in memories if memory.get("active_at_end") is True
    }
    _references(active_declared, memory_ids, f"{label}.active_memory_event_ids_at_end", errors)
    if isinstance(active_declared, list) and set(active_declared) != active_computed:
        errors.append(f"{label} active memory event set mismatch")
    row_ids = bundle.memories.get("expected_active_memory_row_source_event_ids")
    _references(row_ids, active_computed, f"{label}.expected_active_memory_rows", errors)
    if isinstance(row_ids, list) and bundle.memories.get("expected_active_memory_row_count") != len(
        row_ids
    ):
        errors.append(f"{label} expected active memory row count mismatch")
    negative_ids = bundle.memories.get("formation_negative_event_ids")
    _references(negative_ids, memory_ids, f"{label}.formation_negative_event_ids", errors)
    expected_negative = {
        str(memory.get("memory_id")) for memory in memories if memory.get("should_store") is False
    }
    if isinstance(negative_ids, list) and set(negative_ids) != expected_negative:
        errors.append(f"{label} formation negative event set mismatch")

    qa = _object_list(bundle.qa.get("qa"), f"{label}/qa", errors)
    _unique_ids(qa, "question_id", f"{label}/qa", errors)
    if bundle.qa.get("do_not_ingest") is not True:
        errors.append(f"{label}/qa.json must set do_not_ingest=true")
    if bundle.qa.get("evaluation_scope") != {
        "mode": "full_corpus",
        "scenario_group_role": "reporting_slice",
        "memory_family_role": "audit_dimension",
    }:
        errors.append(f"{label}/qa.json must declare the full-corpus evaluation scope")
    for question in qa:
        question_id = str(question.get("question_id", "<unknown>"))
        for field in ("required_memory_ids", "supporting_memory_ids", "history_memory_ids"):
            _references(question.get(field), memory_ids, f"{label}/{question_id}.{field}", errors)
        _references(
            question.get("evidence_turn_ids"),
            set(position),
            f"{label}/{question_id}.evidence_turn_ids",
            errors,
        )
        tier = question.get("evaluation_tier")
        if question.get("hard_gate") is not (tier == "hard_gate"):
            errors.append(f"{label}/{question_id} hard_gate disagrees with evaluation_tier")
        text = question.get("question")
        if isinstance(text, str) and text.strip():
            normalized_questions[_normalize(text)].append(f"{label}:{question_id}")
        else:
            errors.append(f"{label}/{question_id} has a blank question")
    pending_answers = sum(question.get("final_answer") == "TBD_AFTER_KIRA_FILL" for question in qa)
    hard_gate = sum(question.get("evaluation_tier") == "hard_gate" for question in qa)
    diagnostic = sum(question.get("evaluation_tier") == "diagnostic_history" for question in qa)
    counts = {
        "sessions": len(session_pairs),
        "turns": len(turns),
        "fills": len(fills),
        "memory_events": len(memories),
        "qa": len(qa),
        "hard_gate": hard_gate,
        "diagnostic_history": diagnostic,
        "pending_answers": pending_answers,
    }
    for field, actual in counts.items():
        if getattr(entry.counts, field) != actual:
            errors.append(f"{label} manifest count {field} mismatch")
    if entry.materialization_status == "materialized":
        if blank_assistant_ids:
            errors.append(f"{label} is materialized but still has blank assistant turns")
        if pending_answers:
            errors.append(f"{label} is materialized but still has pending answers")
    elif not blank_assistant_ids:
        warnings.append(f"{label} is pending but has no blank assistant turns")
    return counts


def validate_dataset(root: Path) -> ValidationReport:
    dataset_root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        manifest = load_manifest(dataset_root)
    except (OSError, ValidationError, json.JSONDecodeError) as error:
        return ValidationReport(
            dataset_id=None,
            dataset_version=None,
            errors=(f"invalid manifest: {error.__class__.__name__}",),
            warnings=(),
            counts={},
        )
    global_fill_ids: set[str] = set()
    normalized_questions: dict[str, list[str]] = defaultdict(list)
    normalized_facts: dict[str, list[str]] = defaultdict(list)
    totals: Counter[str] = Counter()
    for index in range(len(manifest.bundles)):
        totals.update(
            _validate_bundle(
                dataset_root,
                manifest,
                index,
                errors,
                warnings,
                global_fill_ids,
                normalized_questions,
                normalized_facts,
            )
        )
    for kind, values in (
        ("normalized question", normalized_questions),
        ("normalized canonical fact", normalized_facts),
    ):
        for normalized, locations in values.items():
            if len(locations) > 1:
                errors.append(f"duplicate {kind} at {locations}: {normalized!r}")
    return ValidationReport(
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.dataset_version,
        errors=tuple(errors),
        warnings=tuple(warnings),
        counts=dict(sorted(totals.items())),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="dataset root containing manifest.json")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = validate_dataset(args.root)
    if args.json:
        print(report.as_json())
    else:
        status = "PASS" if report.valid else "FAIL"
        print(f"{status} dataset={report.dataset_id} version={report.dataset_version}")
        if report.counts:
            print("counts " + " ".join(f"{key}={value}" for key, value in report.counts.items()))
        for warning in report.warnings:
            print(f"WARNING {warning}")
        for error in report.errors:
            print(f"ERROR {error}")
    return 0 if report.valid else 1


if __name__ == "__main__":
    sys.exit(main())
