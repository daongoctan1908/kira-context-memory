"""Crash-visible benchmark artifacts and fail-closed resume state.

The store is intentionally local and synthetic-only.  It persists typed IDs, outputs and safe
reason codes; raw exception messages, credentials and connection strings have no artifact field.
"""

import json
import os
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from evaluation.audit import AuditBatch, HumanAuditDecision
from evaluation.isolation import IsolationLedger
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    BenchmarkVariant,
    EvalModel,
    Identifier,
    NonEmpty,
    Outcome,
    Profile,
    RunProvenance,
    Sha256,
    Suite,
)
from evaluation.scoring import (
    FormationMatchDecision,
    FormationMatchVerdict,
    SemanticJudgment,
    output_sha256,
)
from evaluation.timing import TimingReport


class ArtifactRunIdentity(EvalModel):
    contract_id: Literal["kira-week5-benchmark-v4"] = BENCHMARK_CONTRACT_ID
    run_id: UUID
    profile: Profile
    variant: BenchmarkVariant
    provenance: RunProvenance
    dataset_id: Identifier
    dataset_version: str = Field(min_length=1, max_length=64)
    dataset_sha256: Sha256
    compilation_sha256: Sha256
    config_sha256: Sha256
    isolation_sha256: Sha256 | None = None
    seed: int = Field(ge=0, le=2**63 - 1, strict=True)
    suites: tuple[Suite, ...] = Field(min_length=1)
    selected_case_ids: tuple[Identifier, ...] = Field(min_length=1)

    @field_validator("suites", "selected_case_ids")
    @classmethod
    def values_are_unique(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(value) != len(set(value)):
            raise ValueError("run identity selections must be unique")
        return value

    @model_validator(mode="after")
    def variant_matches_provenance(self) -> "ArtifactRunIdentity":
        if self.variant is not self.provenance.variant:
            raise ValueError("artifact variant must match runtime provenance")
        return self


class ArtifactRunManifest(EvalModel):
    schema_version: Literal[1] = 1
    created_at: datetime
    provenance: Literal["synthetic"] = "synthetic"
    environment_role: Literal["mock", "laptop_synthetic", "pc_acceptance", "internal_official"]
    official: bool
    identity: ArtifactRunIdentity

    @model_validator(mode="before")
    @classmethod
    def derive_environment_claims(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        identity = value.get("identity")
        if isinstance(identity, ArtifactRunIdentity):
            profile = identity.profile
        elif isinstance(identity, dict):
            profile = Profile(identity.get("profile"))
        else:
            return value
        roles = {
            Profile.MOCK: "mock",
            Profile.EXTERNAL_SYNTHETIC: "laptop_synthetic",
            Profile.PC_OPENAI_ACCEPTANCE: "pc_acceptance",
            Profile.INTERNAL_TEST: "internal_official",
        }
        expected_role = roles[profile]
        expected_official = profile is Profile.INTERNAL_TEST
        payload = dict(value)
        payload.setdefault("environment_role", expected_role)
        payload.setdefault("official", expected_official)
        if payload["environment_role"] != expected_role:
            raise ValueError("artifact environment role does not match its profile")
        if payload["official"] is not expected_official:
            raise ValueError("only internal_test artifacts may claim official evidence")
        return payload

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact creation timestamp must be timezone-aware")
        return value


class FormedMemoryArtifact(EvalModel):
    """One durable memory mapped back to the formation gold IDs that produced it."""

    case_id: Identifier
    family_id: Identifier
    source_gold_ids: tuple[Identifier, ...] = ()
    logical_user_id: Identifier
    persisted_user_id: Identifier
    memory_id: UUID
    content: NonEmpty
    formation_event_id: UUID
    conversation_id: UUID
    turn_id: Identifier
    boundary_message_id: int = Field(ge=1, strict=True)
    attributed_to: Literal["user", "assistant"] | None = None

    @field_validator("source_gold_ids")
    @classmethod
    def source_gold_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("formed memory source gold IDs must be unique")
        return value


class FormedCorpusCaseArtifact(EvalModel):
    case_id: Identifier
    family_id: Identifier
    logical_user_id: Identifier
    persisted_user_id: Identifier
    formation_event_id: UUID
    source_gold_ids: tuple[Identifier, ...] = ()
    memory_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> "FormedCorpusCaseArtifact":
        if len(self.source_gold_ids) != len(set(self.source_gold_ids)):
            raise ValueError("formed case source gold IDs must be unique")
        if len(self.memory_ids) != len(set(self.memory_ids)):
            raise ValueError("formed case memory IDs must be unique")
        return self


class FormedCorpusArtifact(EvalModel):
    schema_version: Literal[1] = 1
    created_at: datetime
    provenance: Literal["synthetic"] = "synthetic"
    identity: ArtifactRunIdentity
    cases: tuple[FormedCorpusCaseArtifact, ...]
    memories: tuple[FormedMemoryArtifact, ...]

    @field_validator("created_at")
    @classmethod
    def corpus_created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("formed corpus timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def corpus_is_closed_and_owned(self) -> "FormedCorpusArtifact":
        if Suite.FORMATION not in self.identity.suites:
            raise ValueError("formed corpus identity must include the formation suite")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("formed corpus case IDs must be unique")
        if not set(case_ids).issubset(self.identity.selected_case_ids):
            raise ValueError("formed corpus contains a case outside the selected run")
        memory_ids = [memory.memory_id for memory in self.memories]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("formed corpus memory IDs must be unique")
        cases = {case.case_id: case for case in self.cases}
        records_by_case: dict[str, set[UUID]] = {}
        for memory in self.memories:
            case = cases.get(memory.case_id)
            if case is None:
                raise ValueError("formed memory has no owning case")
            if (
                memory.family_id != case.family_id
                or memory.logical_user_id != case.logical_user_id
                or memory.persisted_user_id != case.persisted_user_id
                or memory.formation_event_id != case.formation_event_id
                or memory.source_gold_ids != case.source_gold_ids
            ):
                raise ValueError("formed memory provenance differs from its case")
            records_by_case.setdefault(memory.case_id, set()).add(memory.memory_id)
        for case in self.cases:
            if set(case.memory_ids) != records_by_case.get(case.case_id, set()):
                raise ValueError("formed case memory IDs do not match corpus records")
        return self


class CaseAttemptArtifact(EvalModel):
    schema_version: Literal[1] = 1
    case_id: Identifier
    suite: Suite
    attempt: int = Field(ge=1, strict=True)
    completed_at: datetime
    outcome: Outcome
    output: Any | None = None
    output_sha256: Sha256 | None = None
    reason_codes: tuple[Identifier, ...] = ()
    timing: TimingReport | None = None

    @field_validator("completed_at")
    @classmethod
    def completed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("case attempt timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def output_and_outcome_are_consistent(self) -> "CaseAttemptArtifact":
        if self.output is None:
            if self.output_sha256 is not None:
                raise ValueError("output hash cannot exist without output")
        elif self.output_sha256 != output_sha256(self.output):
            raise ValueError("case output hash does not match output")
        if self.outcome in (Outcome.PASS, Outcome.FAIL, Outcome.REVIEW_REQUIRED):
            if self.output is None:
                raise ValueError("quality outcomes require a valid case output")
        if self.outcome in (Outcome.DEPENDENCY_ERROR, Outcome.PROTOCOL_ERROR, Outcome.NOT_RUN):
            if self.output is not None:
                raise ValueError("execution errors must not persist raw provider output")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("case attempt reason codes must be unique")
        return self


class SemanticJudgmentArtifact(EvalModel):
    kind: Literal["semantic"] = "semantic"
    judgment: SemanticJudgment

    @property
    def case_id(self) -> str:
        return self.judgment.case_id

    @property
    def bound_output_sha256(self) -> str:
        return self.judgment.output_sha256


class FormationJudgmentArtifact(EvalModel):
    kind: Literal["formation"] = "formation"
    case_id: Identifier
    output_sha256: Sha256
    decisions: tuple[FormationMatchDecision, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def prediction_decisions_are_unique(self) -> "FormationJudgmentArtifact":
        indexes = [decision.prediction_index for decision in self.decisions]
        if len(indexes) != len(set(indexes)):
            raise ValueError("formation judgment has duplicate prediction decisions")
        return self

    @property
    def bound_output_sha256(self) -> str:
        return self.output_sha256


JudgmentArtifact = Annotated[
    SemanticJudgmentArtifact | FormationJudgmentArtifact,
    Field(discriminator="kind"),
]


class AuditBatchArtifact(EvalModel):
    kind: Literal["batch"] = "batch"
    batch_id: Identifier
    batch: AuditBatch


class AuditDecisionArtifact(EvalModel):
    kind: Literal["decision"] = "decision"
    batch_id: Identifier
    decision: HumanAuditDecision


AuditArtifact = Annotated[
    AuditBatchArtifact | AuditDecisionArtifact,
    Field(discriminator="kind"),
]


class MetricArtifact(EvalModel):
    name: Identifier
    value: float | None = Field(default=None, allow_inf_nan=False)
    numerator: int = Field(ge=0, strict=True)
    denominator: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def denominator_matches_value(self) -> "MetricArtifact":
        if (self.denominator == 0) != (self.value is None):
            raise ValueError("zero-denominator metrics must be N/A and only those metrics are N/A")
        if self.numerator > self.denominator:
            raise ValueError("metric numerator cannot exceed its denominator")
        return self


class SuiteSummaryArtifact(EvalModel):
    suite: Suite
    eligible: int = Field(ge=0, strict=True)
    attempted: int = Field(ge=0, strict=True)
    scored: int = Field(ge=0, strict=True)
    outcomes: dict[Outcome, int]
    metrics: tuple[MetricArtifact, ...] = ()

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "SuiteSummaryArtifact":
        if self.attempted > self.eligible or self.scored > self.attempted:
            raise ValueError("suite summary counts are inconsistent")
        if any(count < 0 for count in self.outcomes.values()):
            raise ValueError("outcome counts cannot be negative")
        if sum(self.outcomes.values()) != self.attempted:
            raise ValueError("outcome counts must add up to attempted cases")
        names = [metric.name for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("suite metric names must be unique")
        return self


class BenchmarkSummaryArtifact(EvalModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    suites: tuple[SuiteSummaryArtifact, ...]
    safety_passed: bool
    safety_violation_codes: tuple[Identifier, ...] = ()
    pending_judgment: int = Field(ge=0, strict=True)
    pending_audit: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def safety_is_consistent(self) -> "BenchmarkSummaryArtifact":
        if self.safety_passed == bool(self.safety_violation_codes):
            raise ValueError("summary safety state must be the inverse of violations")
        if len(self.safety_violation_codes) != len(set(self.safety_violation_codes)):
            raise ValueError("summary safety violation codes must be unique")
        suites = [summary.suite for summary in self.suites]
        if len(suites) != len(set(suites)):
            raise ValueError("summary suites must be unique")
        return self


class DiagnosticArtifact(EvalModel):
    schema_version: Literal[1] = 1
    case_id: Identifier
    attempt: int = Field(ge=1, strict=True)
    outcome: Literal[Outcome.DEPENDENCY_ERROR, Outcome.PROTOCOL_ERROR]
    reason_codes: tuple[Identifier, ...] = Field(min_length=1)
    trace_id: Identifier | None = None
    correlation_id: Identifier | None = None

    @field_validator("reason_codes")
    @classmethod
    def reason_codes_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("diagnostic reason codes must be unique")
        return value


class ResumePlan(EvalModel):
    run_case_ids: tuple[Identifier, ...]
    pending_judgment_case_ids: tuple[Identifier, ...]
    pending_audit_case_ids: tuple[Identifier, ...]
    ready_case_ids: tuple[Identifier, ...]
    attention_case_ids: tuple[Identifier, ...]


_JUDGMENT_ADAPTER = TypeAdapter(JudgmentArtifact)
_AUDIT_ADAPTER = TypeAdapter(AuditArtifact)


def _canonical_json(model: EvalModel) -> str:
    return json.dumps(
        model.model_dump(mode="json", exclude_none=False, exclude_computed_fields=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_new(path: Path, contents: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _replace(path: Path, contents: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_new(temporary, contents)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_jsonl(path: Path, adapter: TypeAdapter[Any]) -> list[Any]:
    records: list[Any] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith("\n") or not line.strip():
                raise ValueError(f"invalid JSONL record at {path.name}:{line_number}")
            try:
                records.append(adapter.validate_json(line))
            except ValueError as error:
                raise ValueError(f"invalid JSONL record at {path.name}:{line_number}") from error
    return records


class ArtifactStore:
    """Typed append-only evidence plus replaceable derived summaries."""

    def __init__(
        self,
        root: Path,
        manifest: ArtifactRunManifest,
        attempts: list[CaseAttemptArtifact],
        judgments: list[JudgmentArtifact],
        audits: list[AuditArtifact],
    ) -> None:
        self.root = root
        self.manifest = manifest
        self._attempts = attempts
        self._judgments = judgments
        self._audits = audits
        self._validate_loaded_state()

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        identity: ArtifactRunIdentity,
        created_at: datetime,
    ) -> "ArtifactStore":
        manifest = ArtifactRunManifest(created_at=created_at, identity=identity)
        root = root.resolve()
        root.parent.mkdir(parents=True, exist_ok=True)
        temporary = root.with_name(f".{root.name}.{uuid4().hex}.tmp")
        if root.exists():
            raise FileExistsError("artifact run directory already exists")
        try:
            temporary.mkdir()
            (temporary / "diagnostics").mkdir()
            _write_new(temporary / "manifest.json", _canonical_json(manifest) + "\n")
            for name in ("cases.jsonl", "judgments.jsonl", "audits.jsonl"):
                _write_new(temporary / name, "")
            os.rename(temporary, root)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return cls(root, manifest, [], [], [])

    @classmethod
    def resume(
        cls,
        root: Path,
        *,
        expected_identity: ArtifactRunIdentity,
    ) -> "ArtifactStore":
        root = root.resolve()
        manifest = ArtifactRunManifest.model_validate_json(
            (root / "manifest.json").read_text(encoding="utf-8")
        )
        if manifest.identity != expected_identity:
            raise ValueError("resume identity does not match the existing artifact run")
        attempts = _read_jsonl(root / "cases.jsonl", TypeAdapter(CaseAttemptArtifact))
        judgments = _read_jsonl(root / "judgments.jsonl", _JUDGMENT_ADAPTER)
        audits = _read_jsonl(root / "audits.jsonl", _AUDIT_ADAPTER)
        return cls(root, manifest, attempts, judgments, audits)

    def _validate_loaded_state(self) -> None:
        selected = set(self.manifest.identity.selected_case_ids)
        attempts_by_case: dict[str, list[int]] = {}
        hashes_by_case: dict[str, set[str]] = {}
        suites_by_case: dict[str, Suite] = {}
        for attempt in self._attempts:
            if attempt.case_id not in selected:
                raise ValueError("case artifact is outside the selected run corpus")
            if attempt.suite not in self.manifest.identity.suites:
                raise ValueError("case artifact suite is outside the selected run suites")
            attempts_by_case.setdefault(attempt.case_id, []).append(attempt.attempt)
            suites_by_case.setdefault(attempt.case_id, attempt.suite)
            if suites_by_case[attempt.case_id] is not attempt.suite:
                raise ValueError("case suite changed across attempts")
            if attempt.output_sha256:
                hashes_by_case.setdefault(attempt.case_id, set()).add(attempt.output_sha256)
        for attempt_numbers in attempts_by_case.values():
            if sorted(attempt_numbers) != list(range(1, len(attempt_numbers) + 1)):
                raise ValueError("case attempts must be unique and contiguous")

        judgment_keys: list[tuple[str, str]] = []
        for judgment in self._judgments:
            key = (judgment.case_id, judgment.bound_output_sha256)
            if judgment.bound_output_sha256 not in hashes_by_case.get(judgment.case_id, set()):
                raise ValueError("judgment is not bound to a persisted case output")
            expected_suite = (
                judgment.judgment.suite
                if isinstance(judgment, SemanticJudgmentArtifact)
                else Suite.FORMATION
            )
            if suites_by_case.get(judgment.case_id) is not expected_suite:
                raise ValueError("judgment suite does not match the persisted case")
            judgment_keys.append(key)
        if len(judgment_keys) != len(set(judgment_keys)):
            raise ValueError("case output has duplicate semantic judgments")
        judgment_key_set = set(judgment_keys)

        batches: dict[str, AuditBatch] = {}
        decision_keys: list[tuple[str, str, str]] = []
        for artifact in self._audits:
            if isinstance(artifact, AuditBatchArtifact):
                if artifact.batch_id in batches:
                    raise ValueError("audit batch IDs must be unique")
                for selection in artifact.batch.selections:
                    key = (selection.case_id, selection.output_sha256)
                    if key not in judgment_key_set:
                        raise ValueError("audit selection is not bound to a persisted judgment")
                batches[artifact.batch_id] = artifact.batch
                continue
            batch = batches.get(artifact.batch_id)
            if batch is None:
                raise ValueError("audit decision must follow its batch")
            selection = next(
                (
                    item
                    for item in batch.selections
                    if item.case_id == artifact.decision.case_id
                    and item.output_sha256 == artifact.decision.output_sha256
                ),
                None,
            )
            if selection is None:
                raise ValueError("audit decision is not bound to a batch selection")
            decision_keys.append(
                (artifact.batch_id, artifact.decision.case_id, artifact.decision.output_sha256)
            )
        if len(decision_keys) != len(set(decision_keys)):
            raise ValueError("audit selection has duplicate human decisions")

    def _append(self, name: str, artifact: EvalModel) -> None:
        with (self.root / name).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical_json(artifact) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def append_case_attempt(self, attempt: CaseAttemptArtifact) -> None:
        candidate = [*self._attempts, attempt]
        previous = self._attempts
        self._attempts = candidate
        try:
            self._validate_loaded_state()
            self._append("cases.jsonl", attempt)
        except BaseException:
            self._attempts = previous
            raise

    def next_attempt_number(self, case_id: str) -> int:
        if case_id not in self.manifest.identity.selected_case_ids:
            raise ValueError("case is outside the selected run corpus")
        return 1 + sum(attempt.case_id == case_id for attempt in self._attempts)

    def append_judgment(self, judgment: JudgmentArtifact) -> None:
        candidate = [*self._judgments, judgment]
        previous = self._judgments
        self._judgments = candidate
        try:
            self._validate_loaded_state()
            self._append("judgments.jsonl", judgment)
        except BaseException:
            self._judgments = previous
            raise

    def append_audit_batch(self, artifact: AuditBatchArtifact) -> None:
        self._append_audit(artifact)

    def append_audit_decision(self, artifact: AuditDecisionArtifact) -> None:
        self._append_audit(artifact)

    def _append_audit(self, artifact: AuditArtifact) -> None:
        candidate = [*self._audits, artifact]
        previous = self._audits
        self._audits = candidate
        try:
            self._validate_loaded_state()
            self._append("audits.jsonl", artifact)
        except BaseException:
            self._audits = previous
            raise

    def write_summary(self, summary: BenchmarkSummaryArtifact) -> None:
        if summary.run_id != self.manifest.identity.run_id:
            raise ValueError("summary belongs to another run")
        _replace(self.root / "summary.json", _canonical_json(summary) + "\n")

    def write_report(self, markdown: str) -> None:
        if not markdown.strip() or "\x00" in markdown:
            raise ValueError("report must be nonblank text")
        _replace(self.root / "report.md", markdown.rstrip() + "\n")

    def write_diagnostic(self, diagnostic: DiagnosticArtifact) -> Path:
        if diagnostic.case_id not in self.manifest.identity.selected_case_ids:
            raise ValueError("diagnostic case is outside the selected run corpus")
        case_key = output_sha256(diagnostic.case_id)[:16]
        path = self.root / "diagnostics" / f"{case_key}.{diagnostic.attempt}.json"
        _write_new(path, _canonical_json(diagnostic) + "\n")
        return path

    def write_isolation_ledger(self, ledger: IsolationLedger) -> Path:
        """Persist ownership before DB writes so later cleanup never relies on a broad prefix."""

        identity = self.manifest.identity
        if identity.isolation_sha256 is None:
            raise ValueError("artifact run has no isolation plan")
        if ledger.run_id != identity.run_id or ledger.plan_sha256 != identity.isolation_sha256:
            raise ValueError("isolation ledger belongs to another artifact run")
        path = self.root / "diagnostics" / "isolation-ledger.json"
        if path.exists():
            existing = IsolationLedger.model_validate_json(path.read_text(encoding="utf-8"))
            updated = {(item.case_id, item.attempt): item for item in ledger.resources}
            for owned in existing.resources:
                replacement = updated.get((owned.case_id, owned.attempt))
                if replacement is None or (
                    replacement.model_copy(update={"memory_ids": ()})
                    != owned.model_copy(update={"memory_ids": ()})
                ):
                    raise ValueError("persisted resource ownership cannot be removed or reassigned")
                if not set(owned.memory_ids).issubset(replacement.memory_ids):
                    raise ValueError("persisted memory ownership cannot be removed")
        _replace(path, _canonical_json(ledger) + "\n")
        return path

    def write_formed_corpus(self, corpus: FormedCorpusArtifact) -> Path:
        """Persist the provenance-bound corpus consumed by formation-produced retrieval."""

        if corpus.identity != self.manifest.identity:
            raise ValueError("formed corpus belongs to another artifact run")
        path = self.root / "formed-corpus.json"
        _replace(path, _canonical_json(corpus) + "\n")
        return path

    def load_formed_corpus(self) -> FormedCorpusArtifact:
        corpus = FormedCorpusArtifact.model_validate_json(
            (self.root / "formed-corpus.json").read_text(encoding="utf-8")
        )
        if corpus.identity != self.manifest.identity:
            raise ValueError("formed corpus belongs to another artifact run")
        return corpus

    def load_isolation_ledger(self) -> IsolationLedger:
        path = self.root / "diagnostics" / "isolation-ledger.json"
        ledger = IsolationLedger.model_validate_json(path.read_text(encoding="utf-8"))
        identity = self.manifest.identity
        if identity.isolation_sha256 is None:
            raise ValueError("artifact run has no isolation plan")
        if ledger.run_id != identity.run_id or ledger.plan_sha256 != identity.isolation_sha256:
            raise ValueError("isolation ledger belongs to another artifact run")
        return ledger

    def resume_plan(self) -> ResumePlan:
        latest: dict[str, CaseAttemptArtifact] = {}
        for attempt in self._attempts:
            if attempt.case_id not in latest or attempt.attempt > latest[attempt.case_id].attempt:
                latest[attempt.case_id] = attempt
        judgments = {(item.case_id, item.bound_output_sha256): item for item in self._judgments}
        selected_audits: set[tuple[str, str]] = set()
        completed_audits: set[tuple[str, str]] = set()
        gold_errors: set[tuple[str, str]] = set()
        for artifact in self._audits:
            if isinstance(artifact, AuditBatchArtifact):
                selected_audits.update(
                    (selection.case_id, selection.output_sha256)
                    for selection in artifact.batch.selections
                )
            else:
                key = (artifact.decision.case_id, artifact.decision.output_sha256)
                completed_audits.add(key)
                if artifact.decision.disposition.value == "gold_error":
                    gold_errors.add(key)

        run: list[str] = []
        pending_judgment: list[str] = []
        pending_audit: list[str] = []
        ready: list[str] = []
        attention: list[str] = []
        for case_id in self.manifest.identity.selected_case_ids:
            attempt = latest.get(case_id)
            if attempt is None or attempt.outcome in (
                Outcome.NOT_RUN,
                Outcome.DEPENDENCY_ERROR,
                Outcome.PROTOCOL_ERROR,
            ):
                run.append(case_id)
                continue
            if attempt.outcome is Outcome.INSUFFICIENT_EVIDENCE:
                attention.append(case_id)
                continue
            if attempt.outcome in (Outcome.PASS, Outcome.FAIL):
                ready.append(case_id)
                continue
            assert attempt.outcome is Outcome.REVIEW_REQUIRED
            assert attempt.output_sha256 is not None
            key = (case_id, attempt.output_sha256)
            judgment = judgments.get(key)
            if judgment is None:
                pending_judgment.append(case_id)
                continue
            if key in gold_errors:
                attention.append(case_id)
                continue
            uncertain = (
                judgment.judgment.verdict.value == "UNCERTAIN"
                if isinstance(judgment, SemanticJudgmentArtifact)
                else any(
                    decision.verdict is FormationMatchVerdict.UNCERTAIN
                    for decision in judgment.decisions
                )
            )
            if (uncertain or key in selected_audits) and key not in completed_audits:
                pending_audit.append(case_id)
            else:
                ready.append(case_id)
        return ResumePlan(
            run_case_ids=tuple(run),
            pending_judgment_case_ids=tuple(pending_judgment),
            pending_audit_case_ids=tuple(pending_audit),
            ready_case_ids=tuple(ready),
            attention_case_ids=tuple(attention),
        )

    @property
    def latest_outcome_counts(self) -> Counter[Outcome]:
        latest: dict[str, CaseAttemptArtifact] = {}
        for attempt in self._attempts:
            if attempt.case_id not in latest or attempt.attempt > latest[attempt.case_id].attempt:
                latest[attempt.case_id] = attempt
        return Counter(attempt.outcome for attempt in latest.values())

    @property
    def latest_attempts(self) -> tuple[CaseAttemptArtifact, ...]:
        """Return one deterministic latest terminal record per attempted case."""

        latest: dict[str, CaseAttemptArtifact] = {}
        for attempt in self._attempts:
            if attempt.case_id not in latest or attempt.attempt > latest[attempt.case_id].attempt:
                latest[attempt.case_id] = attempt
        return tuple(
            latest[case_id]
            for case_id in self.manifest.identity.selected_case_ids
            if case_id in latest
        )
