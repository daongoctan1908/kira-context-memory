"""Build, checkpoint and apply deterministic KiRa dataset materialization."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from evaluation.dataset import DatasetManifest, default_dataset_root, load_bundle, load_manifest
from evaluation.models import EvalModel, Identifier, NonEmpty

Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_PENDING_ANSWER = "TBD_AFTER_KIRA_FILL"


class MaterializationReference(EvalModel):
    """One dataset field populated from a shared KiRa query result."""

    kind: Literal["conversation_fill", "qa_answer"]
    bundle_id: Identifier
    record_id: Identifier
    target_id: Identifier


class MaterializationTask(EvalModel):
    """One unique provider query and every target that reuses its response."""

    task_id: Sha256
    query: NonEmpty
    references: tuple[MaterializationReference, ...] = Field(min_length=1)
    status: Literal["pending", "completed", "failed"] = "pending"
    attempt_count: int = Field(default=0, ge=0, strict=True)
    response_text: str | None = None
    request_ids: tuple[str, ...] = ()
    message_ids: tuple[str, ...] = ()
    error_class: str | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def state_is_consistent(self) -> MaterializationTask:
        if self.status == "completed":
            if self.response_text is None or not self.response_text.strip():
                raise ValueError("completed materialization task needs a nonblank response")
            if self.error_class is not None or self.completed_at is None:
                raise ValueError("completed materialization task has invalid terminal metadata")
        elif self.response_text is not None or self.completed_at is not None:
            raise ValueError("unfinished materialization task must not contain a response")
        if self.status == "failed" and not self.error_class:
            raise ValueError("failed materialization task needs an error class")
        if self.status != "failed" and self.error_class is not None:
            raise ValueError("only failed materialization tasks may contain an error class")
        return self


class MaterializationCheckpoint(EvalModel):
    """Resume-safe local receipt; it deliberately contains no credentials."""

    schema_version: Literal[1] = 1
    dataset_id: Identifier
    dataset_version: NonEmpty
    source_files: dict[str, Sha256]
    created_at: datetime
    updated_at: datetime
    tasks: tuple[MaterializationTask, ...] = Field(min_length=1)

    @property
    def completed_count(self) -> int:
        return sum(task.status == "completed" for task in self.tasks)

    @property
    def total_count(self) -> int:
        return len(self.tasks)


class MaterializationApplyReport(EvalModel):
    dataset_version: NonEmpty
    unique_queries: int = Field(ge=1)
    conversation_fills: int = Field(ge=0)
    qa_answers: int = Field(ge=0)
    output_root: str


def utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _task_id(query: str) -> str:
    return sha256(query.encode("utf-8")).hexdigest()


def _source_files(root: Path, manifest: DatasetManifest) -> dict[str, str]:
    relative_paths = {"manifest.json"}
    for bundle in manifest.bundles:
        relative_paths.update(
            f"{bundle.path}/{descriptor.name}" for _, descriptor in bundle.files.items()
        )
    return {relative: _digest(root / relative) for relative in sorted(relative_paths)}


def _fill_query(fill: Mapping[str, object]) -> str:
    contract = fill.get("runner_contract")
    if not isinstance(contract, dict):
        raise ValueError("fill has no runner_contract")
    field = contract.get("send_field")
    if field not in {"kira_fill_query", "fill_execution_query"}:
        raise ValueError("fill runner_contract has an unsupported send_field")
    query = fill.get(field)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("fill execution query must not be blank")
    return query


def build_materialization_checkpoint(
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> MaterializationCheckpoint:
    """Compile all fill and pending-QA targets into deduplicated provider tasks."""

    dataset_root = (root or default_dataset_root()).resolve()
    manifest = load_manifest(dataset_root)
    references_by_query: dict[str, list[MaterializationReference]] = defaultdict(list)
    seen_targets: set[tuple[str, str, str]] = set()

    for entry in manifest.bundles:
        bundle = load_bundle(entry, root=dataset_root)
        fills = bundle.fills.get("fills")
        if not isinstance(fills, list):
            raise ValueError(f"{entry.bundle_id}/fills.json has no fills array")
        for fill in fills:
            if not isinstance(fill, dict):
                raise ValueError(f"{entry.bundle_id}/fills.json contains a non-object")
            fill_id = fill.get("fill_id")
            assistant_turn_id = fill.get("assistant_turn_id")
            if not isinstance(fill_id, str) or not isinstance(assistant_turn_id, str):
                raise ValueError(f"{entry.bundle_id} fill target is invalid")
            reference = MaterializationReference(
                kind="conversation_fill",
                bundle_id=entry.bundle_id,
                record_id=fill_id,
                target_id=assistant_turn_id,
            )
            _add_reference(references_by_query, seen_targets, _fill_query(fill), reference)

        questions = bundle.qa.get("qa")
        if not isinstance(questions, list):
            raise ValueError(f"{entry.bundle_id}/qa.json has no qa array")
        for question in questions:
            if not isinstance(question, dict) or question.get("final_answer") != _PENDING_ANSWER:
                continue
            question_id = question.get("question_id")
            query = question.get("gold_rewrite")
            if not isinstance(question_id, str) or not isinstance(query, str) or not query.strip():
                raise ValueError(f"{entry.bundle_id} pending QA target is invalid")
            reference = MaterializationReference(
                kind="qa_answer",
                bundle_id=entry.bundle_id,
                record_id=question_id,
                target_id=question_id,
            )
            _add_reference(references_by_query, seen_targets, query, reference)

    tasks = tuple(
        MaterializationTask(
            task_id=_task_id(query),
            query=query,
            references=tuple(
                sorted(
                    references,
                    key=lambda item: (item.bundle_id, item.kind, item.record_id),
                )
            ),
        )
        for query, references in sorted(
            references_by_query.items(), key=lambda item: _task_id(item[0])
        )
    )
    if not tasks:
        raise ValueError("dataset has no pending materialization targets")
    timestamp = now or utc_now()
    return MaterializationCheckpoint(
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.dataset_version,
        source_files=_source_files(dataset_root, manifest),
        created_at=timestamp,
        updated_at=timestamp,
        tasks=tasks,
    )


def _add_reference(
    destination: dict[str, list[MaterializationReference]],
    seen_targets: set[tuple[str, str, str]],
    query: str,
    reference: MaterializationReference,
) -> None:
    target = (reference.bundle_id, reference.kind, reference.target_id)
    if target in seen_targets:
        raise ValueError(f"duplicate materialization target {target}")
    seen_targets.add(target)
    destination[query].append(reference)


def load_materialization_checkpoint(path: Path) -> MaterializationCheckpoint:
    return MaterializationCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))


def write_materialization_checkpoint(
    path: Path,
    checkpoint: MaterializationCheckpoint,
) -> None:
    """Durably replace a local checkpoint without exposing content on stdout."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(checkpoint.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def verify_checkpoint_source(
    root: Path,
    checkpoint: MaterializationCheckpoint,
) -> None:
    dataset_root = root.resolve()
    manifest = load_manifest(dataset_root)
    if manifest.dataset_id != checkpoint.dataset_id:
        raise ValueError("checkpoint dataset ID differs from source")
    if manifest.dataset_version != checkpoint.dataset_version:
        raise ValueError("checkpoint dataset version differs from source")
    if _source_files(dataset_root, manifest) != checkpoint.source_files:
        raise ValueError("dataset source changed after checkpoint creation")
    expected = build_materialization_checkpoint(dataset_root, now=checkpoint.created_at)
    expected_tasks = tuple((task.task_id, task.query, task.references) for task in expected.tasks)
    actual_tasks = tuple((task.task_id, task.query, task.references) for task in checkpoint.tasks)
    if actual_tasks != expected_tasks:
        raise ValueError("checkpoint task plan differs from dataset source")


def replace_checkpoint_task(
    checkpoint: MaterializationCheckpoint,
    index: int,
    task: MaterializationTask,
    *,
    now: datetime | None = None,
) -> MaterializationCheckpoint:
    tasks = list(checkpoint.tasks)
    tasks[index] = task
    return checkpoint.model_copy(update={"tasks": tuple(tasks), "updated_at": now or utc_now()})


def completed_task(
    task: MaterializationTask,
    response_text: str,
    *,
    request_ids: tuple[str, ...] = (),
    message_ids: tuple[str, ...] = (),
    now: datetime | None = None,
) -> MaterializationTask:
    response = response_text.strip()
    if not response:
        raise ValueError("KiRa response text must not be blank")
    return MaterializationTask(
        task_id=task.task_id,
        query=task.query,
        references=task.references,
        status="completed",
        attempt_count=task.attempt_count + 1,
        response_text=response,
        request_ids=tuple(sorted(set(request_ids))),
        message_ids=tuple(sorted(set(message_ids))),
        completed_at=now or utc_now(),
    )


def failed_task(task: MaterializationTask, error_class: str) -> MaterializationTask:
    safe_class = error_class if error_class.isidentifier() else "MaterializationError"
    return MaterializationTask(
        task_id=task.task_id,
        query=task.query,
        references=task.references,
        status="failed",
        attempt_count=task.attempt_count + 1,
        error_class=safe_class[:128],
    )


def apply_materialization(
    root: Path,
    checkpoint: MaterializationCheckpoint,
    *,
    dataset_version: str,
    output_root: Path | None = None,
) -> MaterializationApplyReport:
    """Apply a complete checkpoint to a validated copy, then optionally sync in place."""

    source_root = root.resolve()
    verify_checkpoint_source(source_root, checkpoint)
    incomplete = [task.task_id for task in checkpoint.tasks if task.status != "completed"]
    if incomplete:
        raise ValueError(f"checkpoint has {len(incomplete)} incomplete tasks")
    destination = output_root.resolve() if output_root is not None else source_root
    if output_root is not None and destination.exists():
        raise FileExistsError("materialized output directory already exists")

    with tempfile.TemporaryDirectory(prefix="kira-materialize-", dir=source_root.parent) as raw:
        staged_root = Path(raw) / source_root.name
        shutil.copytree(source_root, staged_root)
        report = _apply_to_staged_root(
            staged_root,
            checkpoint,
            dataset_version=dataset_version,
        )
        from scripts.benchmark.validate_dataset import validate_dataset

        validation = validate_dataset(staged_root)
        if not validation.valid:
            detail = "; ".join(validation.errors[:5])
            raise ValueError(f"materialized dataset failed deterministic validation: {detail}")
        if output_root is not None:
            shutil.copytree(staged_root, destination)
        else:
            _sync_materialized_files(staged_root, source_root)

    return report.model_copy(update={"output_root": str(destination)})


def _apply_to_staged_root(
    root: Path,
    checkpoint: MaterializationCheckpoint,
    *,
    dataset_version: str,
) -> MaterializationApplyReport:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = {entry["bundle_id"]: entry for entry in manifest["bundles"]}
    bundle_documents: dict[str, dict[str, object]] = {}
    conversation_fills = 0
    qa_answers = 0

    for bundle_id, entry in entries.items():
        bundle_root = root / entry["path"]
        bundle_documents[bundle_id] = {
            "conversation": _read_object(bundle_root / entry["files"]["conversation"]["name"]),
            "qa": _read_object(bundle_root / entry["files"]["qa"]["name"]),
        }

    for task in checkpoint.tasks:
        response = task.response_text
        if response is None:  # pragma: no cover - guarded before staging
            raise ValueError("completed task has no response")
        for reference in task.references:
            documents = bundle_documents[reference.bundle_id]
            if reference.kind == "conversation_fill":
                _set_conversation_turn(
                    documents["conversation"],
                    reference.target_id,
                    response,
                )
                conversation_fills += 1
            else:
                _set_qa_answer(documents["qa"], reference.target_id, response)
                qa_answers += 1

    for bundle_id, entry in entries.items():
        bundle_root = root / entry["path"]
        documents = bundle_documents[bundle_id]
        conversation_path = bundle_root / entry["files"]["conversation"]["name"]
        qa_path = bundle_root / entry["files"]["qa"]["name"]
        _write_json(conversation_path, documents["conversation"])
        _write_json(qa_path, documents["qa"])
        entry["materialization_status"] = "materialized"
        entry["counts"]["pending_answers"] = 0
        for descriptor in entry["files"].values():
            descriptor["sha256"] = _digest(bundle_root / descriptor["name"])

    manifest["dataset_version"] = dataset_version
    manifest["status"] = "materialized"
    _write_json(manifest_path, manifest)
    return MaterializationApplyReport(
        dataset_version=dataset_version,
        unique_queries=checkpoint.total_count,
        conversation_fills=conversation_fills,
        qa_answers=qa_answers,
        output_root=str(root),
    )


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _set_conversation_turn(document: object, turn_id: str, response: str) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("conversation"), dict):
        raise ValueError("conversation document has an invalid shape")
    matches: list[dict[str, object]] = []
    for value in document["conversation"].values():
        if isinstance(value, list):
            matches.extend(
                turn for turn in value if isinstance(turn, dict) and turn.get("dia_id") == turn_id
            )
    if len(matches) != 1:
        raise ValueError(f"assistant target {turn_id} did not resolve exactly once")
    turn = matches[0]
    if turn.get("role") != "assistant" or turn.get("text") not in {"", response}:
        raise ValueError(f"assistant target {turn_id} is not an empty fill slot")
    turn["text"] = response


def _set_qa_answer(document: object, question_id: str, response: str) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("qa"), list):
        raise ValueError("QA document has an invalid shape")
    matches = [
        item
        for item in document["qa"]
        if isinstance(item, dict) and item.get("question_id") == question_id
    ]
    if len(matches) != 1:
        raise ValueError(f"QA target {question_id} did not resolve exactly once")
    question = matches[0]
    if question.get("final_answer") not in {_PENDING_ANSWER, response}:
        raise ValueError(f"QA target {question_id} already has a different final answer")
    if question.get("gold_answer") not in {None, response}:
        raise ValueError(f"QA target {question_id} already has a different gold answer")
    question["gold_answer"] = response
    question["final_answer"] = response


def _sync_materialized_files(staged_root: Path, destination_root: Path) -> None:
    manifest = json.loads((staged_root / "manifest.json").read_text(encoding="utf-8"))
    relative_paths = [Path("manifest.json")]
    for entry in manifest["bundles"]:
        relative_paths.extend(
            Path(entry["path"]) / entry["files"][kind]["name"] for kind in ("conversation", "qa")
        )
    for relative in relative_paths:
        source = staged_root / relative
        target = destination_root / relative
        temporary = target.with_name(target.name + ".materializing")
        temporary.write_bytes(source.read_bytes())
        temporary.replace(target)
