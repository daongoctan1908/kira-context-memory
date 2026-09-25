"""Native Mem0 formation evaluation boundaries.

Write-free evaluation deliberately calls the vendored ``AsyncMemory.add`` pipeline.  It does not
reimplement extraction or parsing, and it refuses clients whose storage/history adapters are not
explicitly marked ephemeral. Persistent evaluation goes through the application use case and then
reads pgvector plus the atomic formation receipt back independently; a provider response alone is
never treated as proof that a memory was committed.
"""

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
import psycopg
from mem0.exceptions import LLMError
from mem0.observability import bind_observer
from psycopg import sql
from pydantic import Field, SecretStr, field_validator, model_validator

from app.domain.errors.memory import (
    LongTermMemoryConnectionError,
    LongTermMemoryOperationError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory import MemoryProcessResult
from app.infrastructure.memory.postgres_admin import (
    formation_receipt_table_name,
    normalize_psycopg_dsn,
)
from app.infrastructure.observability.redaction import masked_json_snapshot
from evaluation.artifacts import (
    ArtifactRunIdentity,
    FormedCorpusArtifact,
    FormedCorpusCaseArtifact,
    FormedMemoryArtifact,
)
from evaluation.models import EvalModel, FormationInput, Identifier, NonEmpty, Outcome


class FormationExecutionStatus(StrEnum):
    VALID_EMPTY = "valid_empty"
    VALID_FACTS = "valid_facts"
    MALFORMED = "malformed"
    PROVIDER_ERROR = "provider_error"
    PROTOCOL_ERROR = "protocol_error"


class MaskedSnapshot(EvalModel):
    value: str | None = None
    truncated: bool = False
    original_bytes: int = Field(default=0, ge=0, strict=True)
    omitted_reason: Identifier | None = None


class FormationStageCapture(EvalModel):
    name: Identifier
    outcome: Identifier = "unknown"
    attributes: dict[Identifier, str | int | float | bool] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    input: MaskedSnapshot | None = None
    output: MaskedSnapshot | None = None


class ExtractedFact(EvalModel):
    text: str = Field(min_length=1)
    attributed_to: str | None = Field(default=None, pattern=r"^(user|assistant)$")
    # Raw LLM scope field, kept unvalidated so INVALID values remain scorable
    # (docs/evaluator-scope-semantics.md section 2).
    scope: object | None = None


class FormationLifecycleRecord(EvalModel):
    event: Literal["ADD"]
    memory_id: UUID
    memory: NonEmpty


class FormationExtractionResult(EvalModel):
    case_id: Identifier
    outcome: Outcome
    status: FormationExecutionStatus
    facts: tuple[ExtractedFact, ...] = ()
    lifecycle_events: tuple[FormationLifecycleRecord, ...] = ()
    provider_calls: int = Field(ge=0, strict=True)
    model: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    stages: tuple[FormationStageCapture, ...]
    reason_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def state_is_consistent(self) -> "FormationExtractionResult":
        valid = self.status in {
            FormationExecutionStatus.VALID_EMPTY,
            FormationExecutionStatus.VALID_FACTS,
        }
        if valid != (self.outcome is Outcome.REVIEW_REQUIRED):
            raise ValueError("only valid extraction results may await quality review")
        if self.status is FormationExecutionStatus.VALID_FACTS and not self.facts:
            raise ValueError("valid-facts result requires at least one fact")
        if self.status is FormationExecutionStatus.VALID_EMPTY and self.facts:
            raise ValueError("valid-empty result cannot contain facts")
        if valid == bool(self.reason_codes):
            raise ValueError("only failed extraction results require reason codes")
        return self


class AsyncFormationClient(Protocol):
    vector_store: object
    db: object

    async def add(self, messages: object, **kwargs: Any) -> object: ...


class MemoryFormationProcessor(Protocol):
    async def execute(
        self,
        reference: CompletedTurnReference,
        formation_event_id: UUID,
    ) -> MemoryProcessResult: ...


class PersistedFormationMemory(EvalModel):
    memory_id: UUID
    content: NonEmpty
    user_id: Identifier
    formation_event_id: UUID
    conversation_id: UUID
    turn_id: Identifier
    boundary_message_id: int = Field(ge=1, strict=True)
    attributed_to: Literal["user", "assistant"] | None = None
    # Enforcement guarantees only valid scopes persist, so the literal is strict.
    memory_scope: Literal["CONVERSATION", "GLOBAL"] | None = None


class FormationReceiptRecord(EvalModel):
    event_id: UUID
    user_id: Identifier
    conversation_id: UUID
    events: tuple[FormationLifecycleRecord, ...]
    memory_count: int = Field(ge=0, strict=True)
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def committed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("formation receipt timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def count_matches_events(self) -> "FormationReceiptRecord":
        if self.memory_count != len(self.events):
            raise ValueError("formation receipt count does not match its result")
        return self


class FormationPersistenceSnapshot(EvalModel):
    event_id: UUID
    user_id: Identifier
    memories: tuple[PersistedFormationMemory, ...] = ()
    receipt: FormationReceiptRecord | None = None

    @model_validator(mode="after")
    def identities_are_consistent(self) -> "FormationPersistenceSnapshot":
        if any(
            memory.formation_event_id != self.event_id or memory.user_id != self.user_id
            for memory in self.memories
        ):
            raise ValueError("persisted memory belongs to another formation identity")
        if self.receipt is not None and (
            self.receipt.event_id != self.event_id or self.receipt.user_id != self.user_id
        ):
            raise ValueError("formation receipt belongs to another formation identity")
        memory_ids = [memory.memory_id for memory in self.memories]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("formation snapshot contains duplicate memory IDs")
        return self


class FormationPersistenceInspector(Protocol):
    async def inspect(self, *, event_id: UUID, user_id: str) -> FormationPersistenceSnapshot: ...


class PersistentFormationResult(EvalModel):
    case_id: Identifier
    family_id: Identifier
    logical_user_id: Identifier
    persisted_user_id: Identifier
    source_gold_ids: tuple[Identifier, ...] = ()
    outcome: Outcome
    extraction: FormationExtractionResult | None = None
    persistence: FormationPersistenceSnapshot | None = None
    reason_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def result_state_is_consistent(self) -> "PersistentFormationResult":
        successful = self.outcome is Outcome.REVIEW_REQUIRED
        if successful:
            if self.extraction is None or self.persistence is None:
                raise ValueError("successful persistent formation requires complete evidence")
            if self.persistence.receipt is None:
                raise ValueError("successful persistent formation requires a receipt")
            if self.reason_codes:
                raise ValueError("successful persistent formation cannot have failure reasons")
        elif not self.reason_codes:
            raise ValueError("failed persistent formation requires a safe reason code")
        if len(self.source_gold_ids) != len(set(self.source_gold_ids)):
            raise ValueError("source gold IDs must be unique")
        return self


def _snapshot(value: object) -> MaskedSnapshot:
    masked, truncated, original_bytes, omitted = masked_json_snapshot(value)
    return MaskedSnapshot(
        value=masked,
        truncated=truncated,
        original_bytes=original_bytes,
        omitted_reason=omitted,
    )


class _CaptureObservation:
    def __init__(self, recorder: "FormationCaptureObserver", index: int) -> None:
        self._recorder = recorder
        self._index = index

    @property
    def _record(self) -> dict[str, object]:
        return self._recorder._records[self._index]

    def set_attribute(self, key: str, value: object) -> None:
        if not isinstance(key, str) or len(key) > 128:
            return
        if (isinstance(value, (int, float, bool)) and not isinstance(value, str)) or (
            isinstance(value, str) and len(value) <= 200
        ):
            attributes = self._record["attributes"]
            assert isinstance(attributes, dict)
            attributes[key] = value

    def set_outcome(self, outcome: str) -> None:
        if isinstance(outcome, str) and outcome:
            self._record["outcome"] = outcome

    def set_input(self, value: object) -> None:
        self._record["input"] = _snapshot(value)

    def set_output(self, value: object) -> None:
        self._record["output"] = _snapshot(value)
        self._recorder._raw_outputs[self._index] = value

    def set_usage(self, usage: Mapping[str, object]) -> None:
        normalized = self._record["usage"]
        assert isinstance(normalized, dict)
        for key in ("input", "output", "total"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                normalized[key] = normalized.get(key, 0) + value


class FormationCaptureObserver:
    """Request-local observer retaining masked artifacts and one in-process parse value."""

    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []
        self._raw_outputs: dict[int, object] = {}

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[_CaptureObservation]:
        del kind
        record: dict[str, object] = {
            "name": name,
            "outcome": "unknown",
            "attributes": {},
            "usage": {},
            "input": None,
            "output": None,
        }
        self._records.append(record)
        observation = _CaptureObservation(self, len(self._records) - 1)
        for key, value in (attributes or {}).items():
            observation.set_attribute(key, value)
        yield observation

    def captures(self) -> tuple[FormationStageCapture, ...]:
        return tuple(FormationStageCapture.model_validate(record) for record in self._records)

    def stage_indexes(self, name: str) -> tuple[int, ...]:
        return tuple(index for index, record in enumerate(self._records) if record["name"] == name)

    def raw_output(self, index: int) -> object | None:
        return self._raw_outputs.get(index)

    def reset(self) -> None:
        """Start one new sequential capture without retaining a previous case's content."""

        self._records.clear()
        self._raw_outputs.clear()


def _require_write_free(client: AsyncFormationClient) -> None:
    if not getattr(client.vector_store, "evaluation_write_free", False):
        raise ValueError("write-free evaluator requires an ephemeral vector store")
    if not getattr(client.db, "evaluation_write_free", False):
        raise ValueError("write-free evaluator requires an ephemeral history store")


def _safe_lifecycle(response: object) -> tuple[FormationLifecycleRecord, ...]:
    if not isinstance(response, Mapping) or set(response) != {"results"}:
        raise ValueError("invalid native formation response")
    rows = response["results"]
    if not isinstance(rows, list):
        raise ValueError("invalid native formation results")
    safe: list[FormationLifecycleRecord] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("invalid native lifecycle event")
        event = row.get("event")
        memory_id = row.get("id")
        memory = row.get("memory")
        if (
            event != "ADD"
            or not isinstance(memory_id, str)
            or not memory_id
            or not isinstance(memory, str)
            or not memory
        ):
            raise ValueError("invalid native lifecycle event")
        safe.append(
            FormationLifecycleRecord(
                event=event,
                memory_id=UUID(memory_id),
                memory=memory,
            )
        )
    return tuple(safe)


def _domain_lifecycle(result: MemoryProcessResult) -> tuple[FormationLifecycleRecord, ...]:
    records: list[FormationLifecycleRecord] = []
    for item in result.events:
        if item.action != "ADD" or item.memory_id is None or item.content is None:
            raise ValueError("persistent formation returned an unsupported lifecycle event")
        records.append(
            FormationLifecycleRecord(
                event="ADD",
                memory_id=UUID(item.memory_id),
                memory=item.content,
            )
        )
    return tuple(records)


def _facts(value: object) -> tuple[ExtractedFact, ...]:
    if not isinstance(value, list):
        raise ValueError("native parse output is not a fact list")
    facts: list[ExtractedFact] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("native parse output contains an invalid fact")
        text = item.get("text")
        attribution = item.get("attributed_to")
        facts.append(ExtractedFact(text=text, attributed_to=attribution, scope=item.get("scope")))
    return tuple(facts)


class PostgresFormationInspector:
    """Read exact event-scoped pgvector rows and receipts without semantic search."""

    def __init__(
        self,
        database_url: SecretStr | str,
        *,
        schema_name: str,
        collection_name: str,
        statement_timeout_ms: int = 5_000,
    ) -> None:
        if not schema_name.strip() or not collection_name.strip():
            raise ValueError("formation inspector identifiers must not be blank")
        if statement_timeout_ms < 1 or isinstance(statement_timeout_ms, bool):
            raise ValueError("formation inspector timeout must be positive")
        raw_url = (
            database_url.get_secret_value() if isinstance(database_url, SecretStr) else database_url
        )
        self._dsn = normalize_psycopg_dsn(raw_url)
        self._schema_name = schema_name
        self._collection_name = collection_name
        self._statement_timeout_ms = statement_timeout_ms

    async def inspect(
        self,
        *,
        event_id: UUID,
        user_id: str,
    ) -> FormationPersistenceSnapshot:
        if not isinstance(event_id, UUID) or not user_id.strip():
            raise ValueError("formation inspection requires an event and user")
        options = (
            f"-c default_transaction_read_only=on -c statement_timeout={self._statement_timeout_ms}"
        )
        memory_rows, receipt_row = await asyncio.to_thread(
            self._read_sync,
            event_id,
            options,
        )

        memories = tuple(
            self._parse_memory(row, expected_event_id=event_id, expected_user_id=user_id)
            for row in memory_rows
        )
        receipt = (
            self._parse_receipt(
                receipt_row,
                expected_event_id=event_id,
                expected_user_id=user_id,
            )
            if receipt_row is not None
            else None
        )
        return FormationPersistenceSnapshot(
            event_id=event_id,
            user_id=user_id,
            memories=memories,
            receipt=receipt,
        )

    def _read_sync(self, event_id: UUID, options: str) -> tuple[list[object], object | None]:
        with (
            psycopg.connect(
                self._dsn,
                autocommit=True,
                options=options,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL(
                    "SELECT id, payload FROM {} "
                    "WHERE payload->>'formation_event_id' = %s ORDER BY id"
                ).format(sql.Identifier(self._schema_name, self._collection_name)),
                (str(event_id),),
            )
            memory_rows = list(cursor.fetchall())
            cursor.execute(
                sql.SQL(
                    "SELECT event_id, user_id, conversation_id, result, memory_count, committed_at "
                    "FROM {} WHERE event_id = %s"
                ).format(
                    sql.Identifier(
                        self._schema_name,
                        formation_receipt_table_name(self._collection_name),
                    )
                ),
                (event_id,),
            )
            return memory_rows, cursor.fetchone()

    @staticmethod
    def _parse_memory(
        row: object,
        *,
        expected_event_id: UUID,
        expected_user_id: str,
    ) -> PersistedFormationMemory:
        if not isinstance(row, Sequence) or len(row) != 2:
            raise ValueError("invalid pgvector formation row")
        memory_id, payload = row
        if not isinstance(memory_id, UUID) or not isinstance(payload, Mapping):
            raise ValueError("invalid pgvector formation row")
        if payload.get("formation_event_id") != str(expected_event_id):
            raise ValueError("pgvector formation event does not match")
        if payload.get("user_id") != expected_user_id:
            raise ValueError("pgvector formation owner does not match")
        return PersistedFormationMemory(
            memory_id=memory_id,
            content=payload.get("data"),
            user_id=payload.get("user_id"),
            formation_event_id=payload.get("formation_event_id"),
            conversation_id=payload.get("conversation_id"),
            turn_id=payload.get("turn_id"),
            boundary_message_id=payload.get("boundary_message_id"),
            attributed_to=payload.get("attributed_to"),
            memory_scope=payload.get("memory_scope"),
        )

    @staticmethod
    def _parse_receipt(
        row: object,
        *,
        expected_event_id: UUID,
        expected_user_id: str,
    ) -> FormationReceiptRecord:
        if not isinstance(row, Sequence) or len(row) != 6:
            raise ValueError("invalid formation receipt row")
        event_id, user_id, conversation_id, raw_result, memory_count, committed_at = row
        if event_id != expected_event_id or user_id != expected_user_id:
            raise ValueError("formation receipt identity does not match")
        events = _safe_lifecycle({"results": raw_result})
        return FormationReceiptRecord(
            event_id=event_id,
            user_id=user_id,
            conversation_id=conversation_id,
            events=events,
            memory_count=memory_count,
            committed_at=committed_at,
        )


def _provider_details(
    recorder: FormationCaptureObserver,
) -> tuple[str | None, dict[str, int]]:
    indexes = recorder.stage_indexes("mem0.extract")
    if not indexes:
        return None, {}
    capture = recorder.captures()[indexes[-1]]
    model = capture.attributes.get("gen_ai.request.model")
    return (model if isinstance(model, str) else None), capture.usage


def _failed_extraction(
    case_id: str,
    recorder: FormationCaptureObserver,
    status: FormationExecutionStatus,
    outcome: Outcome,
    reason_code: str,
) -> FormationExtractionResult:
    model, usage = _provider_details(recorder)
    return FormationExtractionResult(
        case_id=case_id,
        outcome=outcome,
        status=status,
        provider_calls=len(recorder.stage_indexes("mem0.extract")),
        model=model,
        usage=usage,
        stages=recorder.captures(),
        reason_codes=(reason_code,),
    )


def _completed_extraction(
    case_id: str,
    recorder: FormationCaptureObserver,
    lifecycle: tuple[FormationLifecycleRecord, ...],
) -> FormationExtractionResult:
    parse_indexes = recorder.stage_indexes("mem0.extract.parse")
    if len(parse_indexes) != 1:
        return _failed_extraction(
            case_id,
            recorder,
            FormationExecutionStatus.PROTOCOL_ERROR,
            Outcome.PROTOCOL_ERROR,
            "formation_missing_parse_observation",
        )
    parse_index = parse_indexes[0]
    parse_capture = recorder.captures()[parse_index]
    if parse_capture.outcome == "malformed":
        return _failed_extraction(
            case_id,
            recorder,
            FormationExecutionStatus.MALFORMED,
            Outcome.PROTOCOL_ERROR,
            "formation_malformed_extraction",
        )
    try:
        facts = _facts(recorder.raw_output(parse_index))
        if len(lifecycle) > len(facts):
            raise ValueError("native lifecycle exceeds extracted facts")
    except (TypeError, ValueError):
        return _failed_extraction(
            case_id,
            recorder,
            FormationExecutionStatus.PROTOCOL_ERROR,
            Outcome.PROTOCOL_ERROR,
            "formation_invalid_native_output",
        )
    status = FormationExecutionStatus.VALID_FACTS if facts else FormationExecutionStatus.VALID_EMPTY
    model, usage = _provider_details(recorder)
    return FormationExtractionResult(
        case_id=case_id,
        outcome=Outcome.REVIEW_REQUIRED,
        status=status,
        facts=facts,
        lifecycle_events=lifecycle,
        provider_calls=len(recorder.stage_indexes("mem0.extract")),
        model=model,
        usage=usage,
        stages=recorder.captures(),
    )


class WriteFreeFormationEvaluator:
    def __init__(self, client: AsyncFormationClient) -> None:
        _require_write_free(client)
        self._client = client

    async def evaluate(
        self,
        *,
        case_id: str,
        inputs: FormationInput,
        conversation_id: UUID,
        event_id: UUID,
    ) -> FormationExtractionResult:
        recorder = FormationCaptureObserver()
        messages = [
            {"role": message.role, "content": message.content} for message in inputs.messages
        ]
        try:
            with bind_observer(recorder):
                response = await self._client.add(
                    messages,
                    user_id=inputs.user_id,
                    metadata={
                        "formation_event_id": str(event_id),
                        "conversation_id": str(conversation_id),
                        "turn_id": inputs.messages[-1].message_id,
                    },
                    infer=True,
                )
        except (LLMError, httpx.RequestError, TimeoutError):
            return _failed_extraction(
                case_id,
                recorder,
                FormationExecutionStatus.PROVIDER_ERROR,
                Outcome.DEPENDENCY_ERROR,
                "formation_provider_error",
            )
        except Exception:
            return _failed_extraction(
                case_id,
                recorder,
                FormationExecutionStatus.PROTOCOL_ERROR,
                Outcome.PROTOCOL_ERROR,
                "formation_protocol_error",
            )

        try:
            lifecycle = _safe_lifecycle(response)
        except (TypeError, ValueError):
            return _failed_extraction(
                case_id,
                recorder,
                FormationExecutionStatus.PROTOCOL_ERROR,
                Outcome.PROTOCOL_ERROR,
                "formation_invalid_native_output",
            )
        return _completed_extraction(case_id, recorder, lifecycle)


class PersistentFormationEvaluator:
    """Reconcile one fresh application formation against durable pgvector evidence."""

    def __init__(
        self,
        processor: MemoryFormationProcessor,
        inspector: FormationPersistenceInspector,
        observer: FormationCaptureObserver,
    ) -> None:
        self._processor = processor
        self._inspector = inspector
        self._observer = observer
        self._lock = asyncio.Lock()

    async def evaluate(
        self,
        *,
        case_id: str,
        family_id: str,
        source_gold_ids: tuple[str, ...],
        inputs: FormationInput,
        reference: CompletedTurnReference,
        event_id: UUID,
    ) -> PersistentFormationResult:
        if reference.user_id == inputs.user_id:
            raise ValueError("persistent evaluation must use a run-scoped persisted user")
        async with self._lock:
            self._observer.reset()
            before, failure = await self._inspect(
                case_id=case_id,
                family_id=family_id,
                source_gold_ids=source_gold_ids,
                inputs=inputs,
                reference=reference,
                event_id=event_id,
                reason_prefix="formation_preflight",
            )
            if failure is not None:
                return failure
            assert before is not None
            if before.memories or before.receipt is not None:
                return self._failed(
                    case_id=case_id,
                    family_id=family_id,
                    source_gold_ids=source_gold_ids,
                    inputs=inputs,
                    reference=reference,
                    reason_code="formation_state_not_fresh",
                )

            try:
                native_result = await self._processor.execute(reference, event_id)
                lifecycle = _domain_lifecycle(native_result)
            except (
                LongTermMemoryConnectionError,
                LongTermMemoryOperationError,
                LongTermMemoryTimeoutError,
            ):
                extraction = _failed_extraction(
                    case_id,
                    self._observer,
                    FormationExecutionStatus.PROVIDER_ERROR,
                    Outcome.DEPENDENCY_ERROR,
                    "formation_dependency_error",
                )
                return self._failed(
                    case_id=case_id,
                    family_id=family_id,
                    source_gold_ids=source_gold_ids,
                    inputs=inputs,
                    reference=reference,
                    outcome=Outcome.DEPENDENCY_ERROR,
                    reason_code="formation_dependency_error",
                    extraction=extraction,
                )
            except (LongTermMemoryProtocolError, TypeError, ValueError):
                extraction = _failed_extraction(
                    case_id,
                    self._observer,
                    FormationExecutionStatus.PROTOCOL_ERROR,
                    Outcome.PROTOCOL_ERROR,
                    "formation_protocol_error",
                )
                return self._failed(
                    case_id=case_id,
                    family_id=family_id,
                    source_gold_ids=source_gold_ids,
                    inputs=inputs,
                    reference=reference,
                    reason_code="formation_protocol_error",
                    extraction=extraction,
                )
            except Exception:
                extraction = _failed_extraction(
                    case_id,
                    self._observer,
                    FormationExecutionStatus.PROTOCOL_ERROR,
                    Outcome.PROTOCOL_ERROR,
                    "formation_unexpected_error",
                )
                return self._failed(
                    case_id=case_id,
                    family_id=family_id,
                    source_gold_ids=source_gold_ids,
                    inputs=inputs,
                    reference=reference,
                    reason_code="formation_unexpected_error",
                    extraction=extraction,
                )

            extraction = _completed_extraction(case_id, self._observer, lifecycle)
            after, failure = await self._inspect(
                case_id=case_id,
                family_id=family_id,
                source_gold_ids=source_gold_ids,
                inputs=inputs,
                reference=reference,
                event_id=event_id,
                reason_prefix="formation_persistence",
                extraction=extraction,
            )
            if failure is not None:
                return failure
            assert after is not None
            reason = self._reconciliation_failure(
                extraction=extraction,
                persistence=after,
                reference=reference,
            )
            if extraction.outcome is not Outcome.REVIEW_REQUIRED:
                reason = extraction.reason_codes[0]
            if reason is not None:
                return self._failed(
                    case_id=case_id,
                    family_id=family_id,
                    source_gold_ids=source_gold_ids,
                    inputs=inputs,
                    reference=reference,
                    reason_code=reason,
                    extraction=extraction,
                    persistence=after,
                )
            return PersistentFormationResult(
                case_id=case_id,
                family_id=family_id,
                logical_user_id=inputs.user_id,
                persisted_user_id=reference.user_id,
                source_gold_ids=source_gold_ids,
                outcome=Outcome.REVIEW_REQUIRED,
                extraction=extraction,
                persistence=after,
            )

    async def _inspect(
        self,
        *,
        case_id: str,
        family_id: str,
        source_gold_ids: tuple[str, ...],
        inputs: FormationInput,
        reference: CompletedTurnReference,
        event_id: UUID,
        reason_prefix: str,
        extraction: FormationExtractionResult | None = None,
    ) -> tuple[FormationPersistenceSnapshot | None, PersistentFormationResult | None]:
        try:
            snapshot = await self._inspector.inspect(
                event_id=event_id,
                user_id=reference.user_id,
            )
            return snapshot, None
        except (psycopg.OperationalError, OSError, TimeoutError):
            return None, self._failed(
                case_id=case_id,
                family_id=family_id,
                source_gold_ids=source_gold_ids,
                inputs=inputs,
                reference=reference,
                outcome=Outcome.DEPENDENCY_ERROR,
                reason_code=f"{reason_prefix}_unavailable",
                extraction=extraction,
            )
        except Exception:
            return None, self._failed(
                case_id=case_id,
                family_id=family_id,
                source_gold_ids=source_gold_ids,
                inputs=inputs,
                reference=reference,
                reason_code=f"{reason_prefix}_protocol_error",
                extraction=extraction,
            )

    @staticmethod
    def _reconciliation_failure(
        *,
        extraction: FormationExtractionResult,
        persistence: FormationPersistenceSnapshot,
        reference: CompletedTurnReference,
    ) -> str | None:
        receipt = persistence.receipt
        if receipt is None:
            return "formation_receipt_missing"
        if receipt.conversation_id != reference.conversation_id:
            return "formation_receipt_provenance_mismatch"
        if receipt.events != extraction.lifecycle_events:
            return "formation_receipt_lifecycle_mismatch"
        expected = {event.memory_id: event.memory for event in receipt.events}
        actual = {memory.memory_id: memory.content for memory in persistence.memories}
        if actual != expected:
            return "formation_pgvector_receipt_mismatch"
        extracted_texts = {fact.text for fact in extraction.facts}
        if any(content not in extracted_texts for content in actual.values()):
            return "formation_pgvector_extraction_mismatch"
        if any(
            memory.conversation_id != reference.conversation_id
            or memory.turn_id != reference.turn_id
            or memory.boundary_message_id != reference.boundary_message_id
            for memory in persistence.memories
        ):
            return "formation_pgvector_provenance_mismatch"
        return None

    @staticmethod
    def _failed(
        *,
        case_id: str,
        family_id: str,
        source_gold_ids: tuple[str, ...],
        inputs: FormationInput,
        reference: CompletedTurnReference,
        reason_code: str,
        outcome: Outcome = Outcome.PROTOCOL_ERROR,
        extraction: FormationExtractionResult | None = None,
        persistence: FormationPersistenceSnapshot | None = None,
    ) -> PersistentFormationResult:
        return PersistentFormationResult(
            case_id=case_id,
            family_id=family_id,
            logical_user_id=inputs.user_id,
            persisted_user_id=reference.user_id,
            source_gold_ids=source_gold_ids,
            outcome=outcome,
            extraction=extraction,
            persistence=persistence,
            reason_codes=(reason_code,),
        )


def build_formed_corpus(
    *,
    identity: ArtifactRunIdentity,
    results: Sequence[PersistentFormationResult],
    created_at: datetime,
) -> FormedCorpusArtifact:
    """Build a retrieval-ready corpus only from fully reconciled persistent cases."""

    cases: list[FormedCorpusCaseArtifact] = []
    memories: list[FormedMemoryArtifact] = []
    for result in results:
        if result.outcome is not Outcome.REVIEW_REQUIRED or result.persistence is None:
            raise ValueError("formed corpus requires successful reconciled formation results")
        receipt = result.persistence.receipt
        if receipt is None:  # pragma: no cover - enforced by PersistentFormationResult
            raise ValueError("formed corpus result has no durable receipt")
        memory_ids = tuple(memory.memory_id for memory in result.persistence.memories)
        cases.append(
            FormedCorpusCaseArtifact(
                case_id=result.case_id,
                family_id=result.family_id,
                logical_user_id=result.logical_user_id,
                persisted_user_id=result.persisted_user_id,
                formation_event_id=result.persistence.event_id,
                source_gold_ids=result.source_gold_ids,
                memory_ids=memory_ids,
            )
        )
        for memory in result.persistence.memories:
            memories.append(
                FormedMemoryArtifact(
                    case_id=result.case_id,
                    family_id=result.family_id,
                    source_gold_ids=result.source_gold_ids,
                    logical_user_id=result.logical_user_id,
                    persisted_user_id=result.persisted_user_id,
                    memory_id=memory.memory_id,
                    content=memory.content,
                    formation_event_id=memory.formation_event_id,
                    conversation_id=memory.conversation_id,
                    turn_id=memory.turn_id,
                    boundary_message_id=memory.boundary_message_id,
                    attributed_to=memory.attributed_to,
                )
            )
    return FormedCorpusArtifact(
        created_at=created_at,
        identity=identity,
        cases=tuple(cases),
        memories=tuple(memories),
    )
