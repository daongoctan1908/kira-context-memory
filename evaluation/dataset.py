"""Typed access to the canonical KiRa LTM dataset without runtime service imports."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from evaluation.models import EvalModel, GoldReview, Identifier

Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
DatasetVersion = Annotated[str, StringConstraints(min_length=1, max_length=64)]
_SESSION_KEY = re.compile(r"^session_([1-9][0-9]*)$")


class DatasetFile(EvalModel):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+\.json$")]
    sha256: Sha256

    @field_validator("name")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
            raise ValueError("dataset file must be a plain JSON filename")
        return value


class BundleFiles(EvalModel):
    conversation: DatasetFile
    fills: DatasetFile
    memories: DatasetFile
    qa: DatasetFile

    def items(self) -> tuple[tuple[str, DatasetFile], ...]:
        return (
            ("conversation", self.conversation),
            ("fills", self.fills),
            ("memories", self.memories),
            ("qa", self.qa),
        )


class BundleCounts(EvalModel):
    sessions: int = Field(ge=1, strict=True)
    turns: int = Field(ge=1, strict=True)
    fills: int = Field(ge=0, strict=True)
    memory_events: int = Field(ge=0, strict=True)
    qa: int = Field(ge=0, strict=True)
    hard_gate: int = Field(ge=0, strict=True)
    diagnostic_history: int = Field(ge=0, strict=True)
    pending_answers: int = Field(ge=0, strict=True)


class BundleManifest(EvalModel):
    bundle_id: Identifier
    source_conversation_id: Identifier
    revision: int = Field(ge=1, strict=True)
    contract_status: Literal["draft", "frozen"]
    materialization_status: Literal["pending", "materialized"]
    path: Annotated[str, StringConstraints(pattern=r"^bundles/[A-Za-z0-9_.-]+$")]
    review: GoldReview
    counts: BundleCounts
    files: BundleFiles

    @model_validator(mode="after")
    def canonical_path_matches_id(self) -> "BundleManifest":
        if self.path != f"bundles/{self.bundle_id}":
            raise ValueError("bundle path must be bundles/<bundle_id>")
        return self


class TimestampPolicy(EvalModel):
    source: Literal["session_datetime"]
    turn_offset_unit: Literal["microseconds"]
    turn_offset_start: Literal[0]


class DatasetDataPolicy(EvalModel):
    classification: Literal["internal_test_synthetic_derived"]
    external_provider_allowed: bool
    public_git_status: Literal["review_required"]


class DatasetManifest(EvalModel):
    schema_alias: str = Field(alias="$schema")
    schema_version: Literal[1]
    dataset_id: Literal["kira_ltm_v1"]
    dataset_version: DatasetVersion
    status: Literal["draft", "contract_frozen", "materialized", "reviewed", "benchmark_ready"]
    canonical_format: Literal["json"]
    evaluation_scope: Literal["full_corpus"]
    id_namespace: Literal["{bundle_id}:{local_id}"]
    timestamp_policy: TimestampPolicy
    data_policy: DatasetDataPolicy
    bundles: tuple[BundleManifest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def bundle_ids_are_unique(self) -> "DatasetManifest":
        ids = [bundle.bundle_id for bundle in self.bundles]
        if len(set(ids)) != len(ids):
            raise ValueError("bundle IDs must be globally unique")
        return self

    def bundle(self, bundle_id: str) -> BundleManifest:
        for entry in self.bundles:
            if entry.bundle_id == bundle_id:
                return entry
        raise KeyError(bundle_id)


@dataclass(frozen=True, slots=True)
class LoadedBundle:
    manifest: BundleManifest
    conversation: dict[str, Any]
    fills: dict[str, Any]
    memories: dict[str, Any]
    qa: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DatasetMessage:
    bundle_id: str
    session_id: str
    message_id: str
    local_message_id: str
    role: Literal["user", "assistant"]
    content: str
    timestamp: datetime


def default_dataset_root() -> Path:
    return Path(__file__).resolve().parents[1] / "dataset" / "kira_ltm_v1"


def load_manifest(root: Path | None = None) -> DatasetManifest:
    dataset_root = (root or default_dataset_root()).resolve()
    return DatasetManifest.model_validate_json(
        (dataset_root / "manifest.json").read_text(encoding="utf-8")
    )


def namespace_id(bundle_id: str, local_id: str) -> str:
    if not bundle_id or not local_id:
        raise ValueError("bundle and local IDs must not be empty")
    return f"{bundle_id}:{local_id}"


def _safe_child(root: Path, relative: str) -> Path:
    target = (root / PurePosixPath(relative)).resolve()
    if target != root and root not in target.parents:
        raise ValueError("dataset path escapes its root")
    return target


def load_bundle(
    bundle: BundleManifest,
    *,
    root: Path | None = None,
) -> LoadedBundle:
    dataset_root = (root or default_dataset_root()).resolve()
    bundle_root = _safe_child(dataset_root, bundle.path)
    documents: dict[str, dict[str, Any]] = {}
    for kind, descriptor in bundle.files.items():
        path = _safe_child(bundle_root, descriptor.name)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{bundle.bundle_id}/{descriptor.name} must contain a JSON object")
        documents[kind] = value
    return LoadedBundle(
        manifest=bundle,
        conversation=documents["conversation"],
        fills=documents["fills"],
        memories=documents["memories"],
        qa=documents["qa"],
    )


def iter_messages(bundle: LoadedBundle) -> Iterator[DatasetMessage]:
    conversation = bundle.conversation.get("conversation")
    if not isinstance(conversation, dict):
        raise ValueError("conversation document must contain an object named conversation")
    sessions: list[tuple[int, str]] = []
    for key in conversation:
        if match := _SESSION_KEY.fullmatch(key):
            sessions.append((int(match.group(1)), key))
    for session_number, session_key in sorted(sessions):
        source_time = datetime.fromisoformat(str(conversation[f"{session_key}_date_time"]))
        if source_time.tzinfo is None or source_time.utcoffset() is None:
            raise ValueError("session timestamps must be timezone-aware")
        turns = conversation[session_key]
        if not isinstance(turns, list):
            raise ValueError(f"{session_key} must be an array")
        session_id = namespace_id(bundle.manifest.bundle_id, f"session-{session_number:02d}")
        for index, turn in enumerate(turns):
            role = turn["role"]
            if role not in {"user", "assistant"}:
                raise ValueError(f"unsupported role in {session_key}")
            local_id = str(turn["dia_id"])
            yield DatasetMessage(
                bundle_id=bundle.manifest.bundle_id,
                session_id=session_id,
                message_id=namespace_id(bundle.manifest.bundle_id, local_id),
                local_message_id=local_id,
                role=role,
                content=str(turn["text"]),
                timestamp=source_time + timedelta(microseconds=index),
            )
