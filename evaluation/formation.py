"""Native Mem0 formation evaluation boundaries.

Write-free evaluation deliberately calls the vendored ``AsyncMemory.add`` pipeline.  It does not
reimplement extraction or parsing, and it refuses clients whose storage/history adapters are not
explicitly marked ephemeral.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import httpx
from mem0.exceptions import LLMError
from mem0.observability import bind_observer
from pydantic import Field, model_validator

from app.infrastructure.observability.redaction import masked_json_snapshot
from evaluation.models import EvalModel, FormationInput, Identifier, Outcome


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


class FormationExtractionResult(EvalModel):
    case_id: Identifier
    outcome: Outcome
    status: FormationExecutionStatus
    facts: tuple[ExtractedFact, ...] = ()
    lifecycle_events: tuple[dict[str, object], ...] = ()
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


def _require_write_free(client: AsyncFormationClient) -> None:
    if not getattr(client.vector_store, "evaluation_write_free", False):
        raise ValueError("write-free evaluator requires an ephemeral vector store")
    if not getattr(client.db, "evaluation_write_free", False):
        raise ValueError("write-free evaluator requires an ephemeral history store")


def _safe_lifecycle(response: object) -> tuple[dict[str, object], ...]:
    if not isinstance(response, Mapping) or set(response) != {"results"}:
        raise ValueError("invalid native formation response")
    rows = response["results"]
    if not isinstance(rows, list):
        raise ValueError("invalid native formation results")
    safe: list[dict[str, object]] = []
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
        safe.append({"event": event, "id": memory_id, "memory": memory})
    return tuple(safe)


def _facts(value: object) -> tuple[ExtractedFact, ...]:
    if not isinstance(value, list):
        raise ValueError("native parse output is not a fact list")
    facts: list[ExtractedFact] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("native parse output contains an invalid fact")
        text = item.get("text")
        attribution = item.get("attributed_to")
        facts.append(ExtractedFact(text=text, attributed_to=attribution))
    return tuple(facts)


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
            return self._failed(
                case_id,
                recorder,
                FormationExecutionStatus.PROVIDER_ERROR,
                Outcome.DEPENDENCY_ERROR,
                "formation_provider_error",
            )
        except Exception:
            return self._failed(
                case_id,
                recorder,
                FormationExecutionStatus.PROTOCOL_ERROR,
                Outcome.PROTOCOL_ERROR,
                "formation_protocol_error",
            )

        parse_indexes = recorder.stage_indexes("mem0.extract.parse")
        if len(parse_indexes) != 1:
            return self._failed(
                case_id,
                recorder,
                FormationExecutionStatus.PROTOCOL_ERROR,
                Outcome.PROTOCOL_ERROR,
                "formation_missing_parse_observation",
            )
        parse_index = parse_indexes[0]
        parse_capture = recorder.captures()[parse_index]
        if parse_capture.outcome == "malformed":
            return self._failed(
                case_id,
                recorder,
                FormationExecutionStatus.MALFORMED,
                Outcome.PROTOCOL_ERROR,
                "formation_malformed_extraction",
            )
        try:
            facts = _facts(recorder.raw_output(parse_index))
            lifecycle = _safe_lifecycle(response)
            if len(lifecycle) > len(facts):
                raise ValueError("native lifecycle exceeds extracted facts")
        except (TypeError, ValueError):
            return self._failed(
                case_id,
                recorder,
                FormationExecutionStatus.PROTOCOL_ERROR,
                Outcome.PROTOCOL_ERROR,
                "formation_invalid_native_output",
            )
        status = (
            FormationExecutionStatus.VALID_FACTS if facts else FormationExecutionStatus.VALID_EMPTY
        )
        model, usage = self._provider_details(recorder)
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

    @staticmethod
    def _provider_details(
        recorder: FormationCaptureObserver,
    ) -> tuple[str | None, dict[str, int]]:
        indexes = recorder.stage_indexes("mem0.extract")
        if not indexes:
            return None, {}
        capture = recorder.captures()[indexes[-1]]
        model = capture.attributes.get("gen_ai.request.model")
        return (model if isinstance(model, str) else None), capture.usage

    def _failed(
        self,
        case_id: str,
        recorder: FormationCaptureObserver,
        status: FormationExecutionStatus,
        outcome: Outcome,
        reason_code: str,
    ) -> FormationExtractionResult:
        model, usage = self._provider_details(recorder)
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
