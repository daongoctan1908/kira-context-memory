"""Write-free evaluator tests against the vendored native AsyncMemory pipeline."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig
from mem0.observability import (
    current_observation_attribute,
    current_observation_input,
    current_observation_output,
    current_observation_usage,
)

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.infrastructure.memory.mem0_adapter import Mem0Adapter
from evaluation.artifacts import ArtifactRunIdentity, ArtifactStore
from evaluation.formation import (
    ExtractedFact,
    FormationCaptureObserver,
    FormationExecutionStatus,
    FormationExtractionResult,
    FormationPersistenceSnapshot,
    FormationReceiptRecord,
    PersistedFormationMemory,
    PersistentFormationEvaluator,
    PostgresFormationInspector,
    WriteFreeFormationEvaluator,
    build_formed_corpus,
    receipt_provenance_matches,
)
from evaluation.models import (
    HISTORICAL_CONTROL_SHA,
    BenchmarkVariant,
    FormationInput,
    GitSource,
    Message,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)

_CONVERSATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_EVENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_MEMORY_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)


class ScriptedLlm:
    def __init__(self, responses: Sequence[str | BaseException]) -> None:
        self.responses = iter(responses)
        self.calls = 0

    def generate_response(self, *, messages, **_):
        self.calls += 1
        current_observation_attribute("gen_ai.request.model", "internal-memory-model")
        current_observation_input(messages)
        current_observation_usage({"input_tokens": 21, "completion_tokens": 7, "total_tokens": 28})
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        current_observation_output(response)
        return response


def _inputs(content: str = "Tôi muốn báo cáo gửi tới user@example.com") -> FormationInput:
    return FormationInput(
        user_id="synthetic-user",
        messages=(
            Message(
                message_id="message-1",
                session_id="session-1",
                role="user",
                content=content,
            ),
        ),
    )


def _native_memory(responses: Sequence[str | BaseException]):
    store = MagicMock()
    store.evaluation_write_free = True
    store.get_formation_result.return_value = None
    store.search.return_value = []

    def commit(*_, result, **__):
        return True, result

    store.insert_with_formation_receipt.side_effect = commit
    embedder = MagicMock()
    embedder.config = MagicMock(embedding_dims=3)
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    embedder.embed_batch.side_effect = lambda values, _: [[0.4, 0.5, 0.6] for _ in values]
    llm = ScriptedLlm(responses)
    history = MagicMock()
    history.evaluation_write_free = True
    history.get_last_messages.return_value = []
    entity_store = MagicMock()
    entity_store.evaluation_write_free = True

    patches = (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.extract_entities_batch", return_value=[]),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    )
    for active_patch in patches:
        active_patch.start()
    try:
        memory = AsyncMemory(MemoryConfig())
    finally:
        for active_patch in reversed(patches):
            active_patch.stop()
    memory._entity_store = entity_store
    return memory, llm, store, history


@pytest.mark.asyncio
async def test_write_free_evaluator_returns_valid_facts_with_masked_observation():
    response = json.dumps(
        {
            "memory": [
                {
                    "id": "0",
                    "text": "Người dùng muốn báo cáo qua user@example.com",
                    "attributed_to": "user",
                }
            ]
        }
    )
    memory, llm, store, history = _native_memory([response])

    result = await WriteFreeFormationEvaluator(memory).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs(),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert result.status is FormationExecutionStatus.VALID_FACTS
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert [fact.text for fact in result.facts] == ["Người dùng muốn báo cáo qua user@example.com"]
    assert result.provider_calls == llm.calls == 1
    assert result.model == "internal-memory-model"
    assert result.usage == {"input": 21, "output": 7, "total": 28}
    assert len(result.lifecycle_events) == 1
    assert store.insert_with_formation_receipt.call_count == 1
    assert history.save_messages.call_count == 1
    extraction = next(stage for stage in result.stages if stage.name == "mem0.extract")
    assert extraction.input is not None and extraction.output is not None
    assert "user@example.com" not in (extraction.input.value or "")
    assert "user@example.com" not in (extraction.output.value or "")
    assert "[REDACTED_EMAIL]" in (extraction.output.value or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "status", "outcome", "reason"),
    [
        ('{"memory":[]}', FormationExecutionStatus.VALID_EMPTY, Outcome.REVIEW_REQUIRED, None),
        (
            "not-json-at-all",
            FormationExecutionStatus.MALFORMED,
            Outcome.PROTOCOL_ERROR,
            "formation_malformed_extraction",
        ),
        (
            '{"memory":"invalid-list"}',
            FormationExecutionStatus.PROTOCOL_ERROR,
            Outcome.PROTOCOL_ERROR,
            "formation_protocol_error",
        ),
    ],
)
async def test_write_free_evaluator_distinguishes_empty_malformed_and_protocol_error(
    response, status, outcome, reason
):
    memory, _, _, _ = _native_memory([response])

    result = await WriteFreeFormationEvaluator(memory).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs(),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert result.status is status
    assert result.outcome is outcome
    assert result.reason_codes == ((reason,) if reason else ())


@pytest.mark.asyncio
async def test_write_free_evaluator_classifies_provider_failure_without_raw_error():
    memory, llm, _, _ = _native_memory([RuntimeError("secret provider response")])

    result = await WriteFreeFormationEvaluator(memory).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs(),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert result.status is FormationExecutionStatus.PROVIDER_ERROR
    assert result.outcome is Outcome.DEPENDENCY_ERROR
    assert result.reason_codes == ("formation_provider_error",)
    assert result.provider_calls == llm.calls == 1
    assert "secret provider response" not in result.model_dump_json()


def test_write_free_evaluator_refuses_unmarked_storage():
    client = SimpleNamespace(vector_store=object(), db=object())
    with pytest.raises(ValueError, match="ephemeral vector"):
        WriteFreeFormationEvaluator(client)


@pytest.mark.asyncio
async def test_observer_does_not_change_native_provider_calls_or_extraction_behavior():
    response = json.dumps(
        {"memory": [{"id": "0", "text": "Ưu tiên Hà Nội", "attributed_to": "user"}]}
    )
    without_observer, plain_llm, plain_store, _ = _native_memory([response])
    native = await without_observer.add(
        [{"role": "user", "content": "Ưu tiên Hà Nội"}],
        user_id="synthetic-user",
        metadata={
            "formation_event_id": str(_EVENT_ID),
            "conversation_id": str(_CONVERSATION_ID),
            "turn_id": "message-1",
        },
        infer=True,
    )

    with_observer, observed_llm, observed_store, _ = _native_memory([response])
    observed = await WriteFreeFormationEvaluator(with_observer).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs("Ưu tiên Hà Nội"),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert plain_llm.calls == observed_llm.calls == 1
    assert [row["memory"] for row in native["results"]] == [fact.text for fact in observed.facts]
    plain_payload = plain_store.insert_with_formation_receipt.call_args.kwargs["payloads"][0]
    observed_payload = observed_store.insert_with_formation_receipt.call_args.kwargs["payloads"][0]
    for key in ("data", "hash", "user_id", "conversation_id", "turn_id", "attributed_to"):
        assert plain_payload[key] == observed_payload[key]


class _BoundaryStore:
    def __init__(
        self,
        reference: CompletedTurnReference,
        messages: tuple[ConversationMessage, ...],
    ) -> None:
        self.reference = reference
        self.messages = messages
        self.calls = 0

    async def read_through_boundary(
        self,
        user_id: str,
        conversation_id: UUID,
        boundary_message_id: int,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        self.calls += 1
        assert (user_id, conversation_id, boundary_message_id) == (
            self.reference.user_id,
            self.reference.conversation_id,
            self.reference.boundary_message_id,
        )
        assert limit == 10
        return self.messages


class _StateInspector:
    def __init__(
        self,
        store: MagicMock,
        conversation_id: UUID,
        *,
        corrupt_content: bool = False,
    ) -> None:
        self.store = store
        self.conversation_id = conversation_id
        self.corrupt_content = corrupt_content

    async def inspect(self, *, event_id: UUID, user_id: str) -> FormationPersistenceSnapshot:
        state = self.store.formation_state
        raw_receipt = state["receipts"].get(str(event_id))
        rows = [
            (memory_id, payload)
            for memory_id, payload in state["rows"].items()
            if payload["formation_event_id"] == str(event_id)
        ]
        memories = tuple(
            PersistedFormationMemory(
                memory_id=memory_id,
                content=("corrupted" if self.corrupt_content else payload["data"]),
                user_id=payload["user_id"],
                formation_event_id=payload["formation_event_id"],
                conversation_id=payload["conversation_id"],
                turn_id=payload["turn_id"],
                boundary_message_id=payload["boundary_message_id"],
                attributed_to=payload.get("attributed_to"),
            )
            for memory_id, payload in rows
        )
        receipt = None
        if raw_receipt is not None:
            receipt = PostgresFormationInspector._parse_receipt(
                (
                    event_id,
                    user_id,
                    self.conversation_id,
                    raw_receipt,
                    len(raw_receipt),
                    _NOW,
                ),
                expected_event_id=event_id,
                expected_user_id=user_id,
            )
        return FormationPersistenceSnapshot(
            event_id=event_id,
            user_id=user_id,
            memories=memories,
            receipt=receipt,
        )


def _persistent_inputs() -> FormationInput:
    return FormationInput(
        user_id="conv01:user",
        messages=(
            Message(
                message_id="conv01:u1",
                session_id="conv01:session",
                role="user",
                content="Luôn ưu tiên Hà Nội",
                timestamp=_NOW,
            ),
            Message(
                message_id="conv01:a1",
                session_id="conv01:session",
                role="assistant",
                content="Đã ghi nhận",
                timestamp=_NOW,
            ),
        ),
    )


def _persistent_runtime(response: str):
    memory, llm, store, _ = _native_memory([response])
    state: dict[str, dict] = {"rows": {}, "receipts": {}}
    store.formation_state = state

    def get_receipt(event_id: str, user_id: str, conversation_id: str):
        del user_id
        assert conversation_id == str(_CONVERSATION_ID)
        return state["receipts"].get(event_id)

    def commit(vectors, payloads, ids, *, event_id, user_id, conversation_id, result):
        del vectors, user_id
        assert conversation_id == str(_CONVERSATION_ID)
        if event_id in state["receipts"]:
            return False, state["receipts"][event_id]
        state["receipts"][event_id] = result
        state["rows"].update(
            {UUID(memory_id): payload for memory_id, payload in zip(ids, payloads, strict=True)}
        )
        return True, result

    store.get_formation_result.side_effect = get_receipt
    store.insert_with_formation_receipt.side_effect = commit
    reference = CompletedTurnReference(
        user_id="eval:run:user-1",
        session_id="eval-session-1",
        conversation_id=_CONVERSATION_ID,
        turn_id="eval-turn-1",
        boundary_message_id=2,
    )
    messages = (
        ConversationMessage(
            reference.session_id,
            reference.turn_id,
            ConversationRole.USER,
            "Luôn ưu tiên Hà Nội",
            _NOW,
        ),
        ConversationMessage(
            reference.session_id,
            reference.turn_id,
            ConversationRole.ASSISTANT,
            "Đã ghi nhận",
            _NOW,
        ),
    )
    observer = FormationCaptureObserver()
    adapter = Mem0Adapter(
        memory,
        search_timeout_seconds=1,
        operation_timeout_seconds=1,
        observer=observer,
    )
    processor = ProcessMemoryUseCase(_BoundaryStore(reference, messages), adapter)
    inspector = _StateInspector(store, reference.conversation_id)
    return processor, observer, inspector, reference, llm, store


def _formation_identity() -> ArtifactRunIdentity:
    source = GitSource(sha="1" * 40, dirty=False)
    return ArtifactRunIdentity(
        run_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        profile=Profile.INTERNAL_TEST,
        variant=BenchmarkVariant.WORKING_TREE,
        provenance=RunProvenance(
            variant=BenchmarkVariant.WORKING_TREE,
            runtime=source,
            harness=source,
            prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
            package_versions={
                "kira-context-memory": "0.4.1",
                "viettel-mem0": "2.0.20+viettel.6",
            },
        ),
        dataset_id="kira-ltm-v1",
        dataset_version="1.0.0-test",
        dataset_sha256="c" * 64,
        compilation_sha256="d" * 64,
        config_sha256="e" * 64,
        seed=742,
        suites=(Suite.FORMATION,),
        selected_case_ids=("conv01:formation:M01",),
    )


@pytest.mark.asyncio
async def test_persistent_evaluator_reconciles_application_mem0_rows_and_receipt(
    tmp_path: Path,
):
    response = json.dumps(
        {"memory": [{"id": "0", "text": "Ưu tiên Hà Nội", "attributed_to": "user"}]}
    )
    processor, observer, inspector, reference, llm, store = _persistent_runtime(response)

    result = await PersistentFormationEvaluator(processor, inspector, observer).evaluate(
        case_id="conv01:formation:M01",
        family_id="conv01:memory-family:preference",
        source_gold_ids=("conv01:M01",),
        inputs=_persistent_inputs(),
        reference=reference,
        event_id=_EVENT_ID,
    )

    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert result.extraction is not None
    assert [fact.text for fact in result.extraction.facts] == ["Ưu tiên Hà Nội"]
    assert result.persistence is not None and result.persistence.receipt is not None
    assert result.persistence.receipt.events == result.extraction.lifecycle_events
    assert len(result.persistence.memories) == 1
    assert result.persistence.memories[0].content == "Ưu tiên Hà Nội"
    assert result.persistence.memories[0].conversation_id == reference.conversation_id
    assert llm.calls == 1
    assert store.insert_with_formation_receipt.call_count == 1

    identity = _formation_identity()
    corpus = build_formed_corpus(identity=identity, results=(result,), created_at=_NOW)
    artifact_store = ArtifactStore.create(tmp_path / "run", identity=identity, created_at=_NOW)
    path = artifact_store.write_formed_corpus(corpus)
    loaded = artifact_store.load_formed_corpus()

    assert path.name == "formed-corpus.json"
    assert loaded.identity.provenance == identity.provenance
    assert loaded.cases[0].source_gold_ids == ("conv01:M01",)
    assert loaded.memories[0].memory_id == result.persistence.memories[0].memory_id
    assert loaded.memories[0].persisted_user_id == reference.user_id


@pytest.mark.asyncio
async def test_persistent_evaluator_commits_and_reconciles_valid_empty_receipt():
    processor, observer, inspector, reference, llm, store = _persistent_runtime('{"memory":[]}')

    result = await PersistentFormationEvaluator(processor, inspector, observer).evaluate(
        case_id="conv01:formation:M01",
        family_id="conv01:memory-family:negative",
        source_gold_ids=(),
        inputs=_persistent_inputs(),
        reference=reference,
        event_id=_EVENT_ID,
    )

    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert result.extraction is not None
    assert result.extraction.status is FormationExecutionStatus.VALID_EMPTY
    assert result.persistence is not None
    assert result.persistence.memories == ()
    assert result.persistence.receipt is not None
    assert result.persistence.receipt.memory_count == 0
    assert llm.calls == 1
    assert store.insert_with_formation_receipt.call_count == 1


@pytest.mark.asyncio
async def test_persistent_evaluator_fails_closed_on_row_receipt_mismatch():
    response = json.dumps(
        {"memory": [{"id": "0", "text": "Ưu tiên Hà Nội", "attributed_to": "user"}]}
    )
    processor, observer, inspector, reference, _, _ = _persistent_runtime(response)
    inspector.corrupt_content = True

    result = await PersistentFormationEvaluator(processor, inspector, observer).evaluate(
        case_id="conv01:formation:M01",
        family_id="conv01:memory-family:preference",
        source_gold_ids=("conv01:M01",),
        inputs=_persistent_inputs(),
        reference=reference,
        event_id=_EVENT_ID,
    )

    assert result.outcome is Outcome.PROTOCOL_ERROR
    assert result.reason_codes == ("formation_pgvector_receipt_mismatch",)


@pytest.mark.asyncio
async def test_persistent_evaluator_refuses_nonfresh_event_without_calling_processor():
    processor = AsyncMock()
    observer = FormationCaptureObserver()
    reference = _persistent_runtime('{"memory":[]}')[3]
    receipt = FormationReceiptRecord(
        event_id=_EVENT_ID,
        user_id=reference.user_id,
        conversation_id=reference.conversation_id,
        events=(),
        memory_count=0,
        committed_at=_NOW,
    )
    inspector = AsyncMock()
    inspector.inspect.return_value = FormationPersistenceSnapshot(
        event_id=_EVENT_ID,
        user_id=reference.user_id,
        receipt=receipt,
    )

    result = await PersistentFormationEvaluator(processor, inspector, observer).evaluate(
        case_id="conv01:formation:M01",
        family_id="conv01:memory-family:negative",
        source_gold_ids=(),
        inputs=_persistent_inputs(),
        reference=reference,
        event_id=_EVENT_ID,
    )

    assert result.outcome is Outcome.PROTOCOL_ERROR
    assert result.reason_codes == ("formation_state_not_fresh",)
    processor.execute.assert_not_awaited()


def test_postgres_inspector_parses_only_exact_provenance_and_receipt_contract():
    payload = {
        "data": "Ưu tiên Hà Nội",
        "user_id": "eval:run:user-1",
        "formation_event_id": str(_EVENT_ID),
        "conversation_id": str(_CONVERSATION_ID),
        "turn_id": "eval-turn-1",
        "boundary_message_id": 2,
        "attributed_to": "user",
    }
    memory = PostgresFormationInspector._parse_memory(
        (_MEMORY_ID, payload),
        expected_event_id=_EVENT_ID,
        expected_user_id="eval:run:user-1",
    )
    receipt = PostgresFormationInspector._parse_receipt(
        (
            _EVENT_ID,
            "eval:run:user-1",
            _CONVERSATION_ID,
            [{"id": str(_MEMORY_ID), "memory": "Ưu tiên Hà Nội", "event": "ADD"}],
            1,
            _NOW,
        ),
        expected_event_id=_EVENT_ID,
        expected_user_id="eval:run:user-1",
    )

    assert memory.memory_id == _MEMORY_ID
    assert receipt.events[0].memory_id == _MEMORY_ID
    with pytest.raises(ValueError, match="owner"):
        PostgresFormationInspector._parse_memory(
            (_MEMORY_ID, payload),
            expected_event_id=_EVENT_ID,
            expected_user_id="another-user",
        )


def _control_provenance() -> RunProvenance:
    return RunProvenance(
        variant=BenchmarkVariant.HISTORICAL_CONTROL,
        runtime=GitSource(sha=HISTORICAL_CONTROL_SHA, dirty=False),
        harness=GitSource(sha="a" * 40, dirty=False),
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={"kira-context-memory": "0.4.1", "viettel-mem0": "2.0.20+viettel.3"},
    )


def _legacy_receipt_row(*, user_id="eval:run:user-1", result=()):
    return (_EVENT_ID, user_id, list(result), len(result), _NOW)


def test_legacy_receipt_contract_requires_explicit_control_and_never_infers_conversation():
    row = _legacy_receipt_row()
    with pytest.raises(ValueError, match="invalid formation receipt row"):
        PostgresFormationInspector._parse_receipt(
            row, expected_event_id=_EVENT_ID, expected_user_id="eval:run:user-1"
        )
    receipt = PostgresFormationInspector._parse_receipt(
        row,
        expected_event_id=_EVENT_ID,
        expected_user_id="eval:run:user-1",
        historical_control_runtime=True,
    )
    assert receipt.conversation_id is None
    assert receipt.receipt_contract == "historical_control_75deb1d8"
    assert receipt.memory_count == 0
    with pytest.raises(ValueError, match="identity"):
        PostgresFormationInspector._parse_receipt(
            row,
            expected_event_id=_EVENT_ID,
            expected_user_id="another-user",
            historical_control_runtime=True,
        )
    with pytest.raises(ValueError, match="conversation provenance"):
        FormationReceiptRecord(
            event_id=_EVENT_ID,
            user_id="eval:run:user-1",
            conversation_id=None,
            events=(),
            memory_count=0,
            committed_at=_NOW,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_control", [False, True])
async def test_inspector_sql_uses_only_the_receipt_columns_of_declared_runtime(historical_control):
    inspector = PostgresFormationInspector(
        "postgresql://local.invalid/test",
        schema_name="eval_schema",
        collection_name="memories",
        runtime_provenance=_control_provenance() if historical_control else None,
    )
    user_id = "eval:run:user-1"
    row = (
        _legacy_receipt_row()
        if historical_control
        else (_EVENT_ID, user_id, _CONVERSATION_ID, [], 0, _NOW)
    )
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = row
    with patch("evaluation.formation.psycopg.connect", return_value=connection) as connect:
        snapshot = await inspector.inspect(event_id=_EVENT_ID, user_id=user_id)
    receipt_sql = cursor.execute.call_args_list[-1].args[0].as_string()
    assert ("conversation_id" in receipt_sql) is not historical_control
    assert "default_transaction_read_only=on" in connect.call_args.kwargs["options"]
    assert snapshot.receipt.conversation_id == (None if historical_control else _CONVERSATION_ID)
    assert inspector.historical_control_runtime is historical_control
    working_tree = _control_provenance().model_copy(
        update={"variant": BenchmarkVariant.WORKING_TREE}
    )
    strict = PostgresFormationInspector(
        "postgresql://local.invalid/test",
        schema_name="eval_schema",
        collection_name="memories",
        runtime_provenance=working_tree,
    )
    assert not strict.historical_control_runtime


def test_legacy_empty_receipt_reconciliation_checks_actual_event_owner_contract():
    reference = _persistent_runtime('{"memory":[]}')[3]
    receipt = PostgresFormationInspector._parse_receipt(
        _legacy_receipt_row(user_id=reference.user_id),
        expected_event_id=_EVENT_ID,
        expected_user_id=reference.user_id,
        historical_control_runtime=True,
    )
    snapshot = FormationPersistenceSnapshot(
        event_id=_EVENT_ID, user_id=reference.user_id, receipt=receipt
    )
    extraction = FormationExtractionResult(
        case_id="conv01:formation:negative",
        outcome=Outcome.REVIEW_REQUIRED,
        status=FormationExecutionStatus.VALID_EMPTY,
        provider_calls=1,
        stages=(),
    )
    assert not receipt_provenance_matches(receipt, reference)
    assert receipt_provenance_matches(receipt, reference, historical_control_runtime=True)
    assert (
        PersistentFormationEvaluator._reconciliation_failure(
            extraction=extraction, persistence=snapshot, reference=reference
        )
        == "formation_receipt_provenance_mismatch"
    )
    assert (
        PersistentFormationEvaluator._reconciliation_failure(
            extraction=extraction,
            persistence=snapshot,
            reference=reference,
            historical_control_runtime=True,
        )
        is None
    )


def test_legacy_compatibility_does_not_relax_persisted_memory_provenance():
    from evaluation.formation import FormationLifecycleRecord

    reference = _persistent_runtime('{"memory":[]}')[3]
    text = "Ưu tiên Hà Nội"
    event = FormationLifecycleRecord(event="ADD", memory_id=_MEMORY_ID, memory=text)
    receipt = PostgresFormationInspector._parse_receipt(
        _legacy_receipt_row(
            user_id=reference.user_id,
            result=({"id": str(_MEMORY_ID), "event": "ADD", "memory": text},),
        ),
        expected_event_id=_EVENT_ID,
        expected_user_id=reference.user_id,
        historical_control_runtime=True,
    )
    snapshot = FormationPersistenceSnapshot(
        event_id=_EVENT_ID,
        user_id=reference.user_id,
        receipt=receipt,
        memories=(
            PersistedFormationMemory(
                memory_id=_MEMORY_ID,
                content=text,
                user_id=reference.user_id,
                formation_event_id=_EVENT_ID,
                conversation_id=UUID(int=9),
                turn_id=reference.turn_id,
                boundary_message_id=reference.boundary_message_id,
            ),
        ),
    )
    extraction = FormationExtractionResult(
        case_id="conv01:formation:M01",
        outcome=Outcome.REVIEW_REQUIRED,
        status=FormationExecutionStatus.VALID_FACTS,
        facts=(ExtractedFact(text=text),),
        lifecycle_events=(event,),
        provider_calls=1,
        stages=(),
    )
    assert (
        PersistentFormationEvaluator._reconciliation_failure(
            extraction=extraction,
            persistence=snapshot,
            reference=reference,
            historical_control_runtime=True,
        )
        == "formation_pgvector_provenance_mismatch"
    )
