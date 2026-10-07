"""Provider-free tests for native runtime composition and crash ownership."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobResult,
    ProcessMemoryJobUseCase,
)
from app.config.settings import Settings
from app.domain.errors.memory import LongTermMemoryConnectionError
from app.domain.models.conversation import AppendTurnResult, CompletedTurnReference
from app.domain.models.kira import KiraEventKind, KiraStreamEvent
from app.domain.models.memory import LongTermMemory, MemoryLifecycleEvent, MemoryProcessResult
from app.domain.models.memory_job import MemoryJob
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import memory_jobs
from evaluation.artifacts import ArtifactRunIdentity, ArtifactStore
from evaluation.compiler import compile_dataset
from evaluation.config import EvalConfig, ProviderConfig, load_config
from evaluation.cross_session import (
    CrossSessionCondition,
    CrossSessionDependencyError,
    CrossSessionProtocolError,
    MemoryReadinessStatus,
)
from evaluation.dataset import default_dataset_root
from evaluation.formation import (
    FormationLifecycleRecord,
    FormationPersistenceSnapshot,
    FormationReceiptRecord,
    PersistedFormationMemory,
)
from evaluation.isolation import (
    IsolationPlan,
    allocate_bundle_qa_resources,
    allocate_case_resources,
    create_isolation_plan,
    isolation_plan_sha256,
    new_isolation_ledger,
    register_case_resources,
)
from evaluation.models import (
    BenchmarkVariant,
    CrossSessionInput,
    EvalCase,
    FormationInput,
    GitSource,
    GoldFact,
    GoldSpecification,
    Message,
    Outcome,
    Profile,
    RunProvenance,
    Suite,
)
from evaluation.native_runtime import (
    NativeCrossSessionRuntime,
    NativeRuntimeResources,
    PersistentFormationRuntime,
    PersistentRetrievalRuntime,
    _NoOpContextObserver,
    _owned_turn_id,
    _persist_message_pairs,
    _RecordingKira,
    _RecordingMemory,
    _RecordingRewriter,
    _SourceMemoryJobQueue,
    create_native_runtime,
    reconcile_interrupted_attempts,
    settings_for_native_runtime,
)
from evaluation.runner import create_or_resume_store, prepare_benchmark_run

_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _base_settings() -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="https://kira.invalid",
        kira_username="eval-user",
        kira_basic_auth="secret",
    )


def _native_config() -> EvalConfig:
    extraction = ProviderConfig(
        base_url="https://extract.invalid/v1",
        model="extract-model",
        api_key="extract-key",
        auth_required=True,
    )
    rewrite = ProviderConfig(
        base_url="https://rewrite.invalid/v1",
        model="rewrite-model",
        api_key="rewrite-key",
        auth_required=True,
    )
    embedding = ProviderConfig(
        base_url="https://embed.invalid/v1",
        model="embed-model",
        api_key="embed-key",
        auth_required=True,
    )
    judge = ProviderConfig(
        base_url="https://judge.invalid/v1",
        model="judge-model",
        api_key="judge-key",
        auth_required=True,
    )
    return EvalConfig(
        database_url="postgresql://eval:secret@localhost/eval",
        memory_database_url="postgresql://eval:secret@localhost/eval",
        extraction=extraction,
        rewrite=rewrite,
        embedding=embedding,
        judge=judge,
        embedding_dimensions=1536,
    )


def _provenance() -> RunProvenance:
    source = GitSource(sha="1" * 40, dirty=False)
    return RunProvenance(
        variant=BenchmarkVariant.WORKING_TREE,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": "a" * 64, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": "2.0.20+viettel.6",
        },
    )


def _formation_case() -> EvalCase:
    return EvalCase(
        case_id="conv01:formation:M01",
        family_id="conv01:memory-family:preference",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        source_row_ids=("conv01:conversation:t1",),
        inputs=FormationInput(
            user_id="conv01:user",
            messages=(
                Message(message_id="turn-1", role="user", content="Ưu tiên Hà Nội"),
                Message(message_id="turn-1-a", role="assistant", content="Đã ghi nhận"),
            ),
        ),
        gold=GoldSpecification(
            facts=(
                GoldFact(
                    gold_id="conv01:M01",
                    text="Ưu tiên Hà Nội",
                    evidence_message_ids=("turn-1",),
                    attributed_to="user",
                ),
            ),
            semantic_expectation="Remember the location preference.",
        ),
    )


def _cross_case() -> EvalCase:
    return EvalCase(
        case_id="conv01:cross-session:Q01",
        family_id="conv01:scenario:recall",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        source_row_ids=("conv01:conversation:t1", "conv01:qa:Q01"),
        inputs=CrossSessionInput(
            user_id="conv01:user",
            session_a="session-a",
            session_b="session-b",
            session_a_messages=(
                Message(message_id="turn-a", role="user", content="Ưu tiên Hà Nội"),
                Message(message_id="turn-a-answer", role="assistant", content="Đã ghi nhận"),
            ),
            session_b_query="Tôi ưu tiên ở đâu?",
            source_session_ids=("session-a",),
        ),
        gold=GoldSpecification(
            relevant_memory_ids=("conv01:M01",),
            semantic_expectation="Answer with the remembered location.",
            expected_answer="Bạn ưu tiên Hà Nội.",
        ),
    )


def _isolated_store(
    tmp_path: Path, case: EvalCase, *, additional_case_ids: tuple[str, ...] = ()
) -> tuple[ArtifactStore, IsolationPlan]:
    plan = create_isolation_plan(
        run_id=_RUN_ID,
        owner_token=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        conversation_database_url="postgresql://eval:secret@localhost/eval",
        memory_database_url="postgresql://eval:secret@localhost/eval",
    )
    identity = ArtifactRunIdentity(
        run_id=_RUN_ID,
        profile=Profile.MOCK,
        variant=BenchmarkVariant.WORKING_TREE,
        provenance=_provenance(),
        dataset_id="kira-ltm-v1",
        dataset_version="test",
        dataset_sha256="c" * 64,
        compilation_sha256="d" * 64,
        config_sha256="e" * 64,
        isolation_sha256=isolation_plan_sha256(plan),
        seed=742,
        suites=(case.suite,),
        selected_case_ids=(case.case_id, *additional_case_ids),
    )
    store = ArtifactStore.create(
        tmp_path / "owned-run",
        identity=identity,
        created_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    store.write_isolation_plan(plan)
    store.write_isolation_ledger(new_isolation_ledger(plan))
    return store, plan


def test_native_settings_use_only_the_explicit_evaluated_providers():
    base = _base_settings()
    config = _native_config()

    settings = settings_for_native_runtime(base, config)

    assert str(settings.vllm_base_url) == "https://rewrite.invalid/v1"
    assert settings.vllm_model == "rewrite-model"
    assert settings.memory_llm_model == "extract-model"
    assert settings.memory_embedding_model == "embed-model"
    assert settings.memory_embedding_dims == 1536
    assert settings.memory_schema == config.memory_schema
    assert settings.otel_enabled is False
    assert base.vllm_model is None

    with pytest.raises(ValueError, match="explicit"):
        settings_for_native_runtime(base, config.model_copy(update={"rewrite": ProviderConfig()}))


class _Memory:
    def __init__(self) -> None:
        self.closed = 0
        self.processed: list[object] = []

    async def search(self, user_id, query, *, top_k, threshold):
        del user_id, query, top_k, threshold
        return (LongTermMemory("memory-1", "Hà Nội", 0.9),)

    async def search_scoped(self, user_id, query, *, conversation_id, scope, top_k, threshold):
        del user_id, query, conversation_id, scope, top_k, threshold
        return (LongTermMemory("memory-1", "Hà Nội", 0.9),)

    async def process_memory(self, source):
        self.processed.append(source)
        return "processed"

    def close(self) -> None:
        self.closed += 1


class _Rewriter:
    async def rewrite(self, context):
        return f"rewritten:{context}"


class _Kira:
    def __init__(self) -> None:
        self.invalidated = 0

    async def chat_stream(self, message):
        async def stream():
            yield KiraStreamEvent(
                kind=KiraEventKind.TEXT,
                raw_data="{}",
                payload={},
                text_fragment=message,
            )

        return stream()

    async def invalidate_token(self):
        self.invalidated += 1


async def test_recording_ports_and_noop_observer_preserve_business_values():
    memory_backend = _Memory()
    memory = _RecordingMemory(memory_backend)  # type: ignore[arg-type]
    returned = await memory.search("user", "query", top_k=3, threshold=0.1)
    assert returned == memory.returned
    assert await memory.process_memory("source") == "processed"

    disabled = _RecordingMemory(None)
    assert await disabled.search("user", "query", top_k=3, threshold=0.1) == ()
    with pytest.raises(RuntimeError, match="disabled"):
        await disabled.process_memory("source")

    rewriter = _RecordingRewriter(_Rewriter())  # type: ignore[arg-type]
    assert await rewriter.rewrite("query") == "rewritten:query"
    assert rewriter.output == "rewritten:query"

    backend = _Kira()
    kira = _RecordingKira(backend)  # type: ignore[arg-type]
    stream = await kira.chat_stream("hello")
    assert [event.text_fragment async for event in stream] == ["hello"]
    assert kira.messages == ["hello"]
    assert len(kira.events) == 1
    await kira.invalidate_token()
    assert backend.invalidated == 1

    observer = _NoOpContextObserver()
    with observer.stage("retrieval") as stage:
        stage.set_attribute("key", "value")
        stage.set_outcome("PASS")
        stage.set_input({"secret": "value"})
        stage.set_output("output")
        stage.set_usage({"total": 1})
    observer.request_attribute("key", "value")
    observer.capture_telemetry_context("correlation")
    observer.context_observed(1, 2)
    observer.memory_search_observed("hit", 1, 0.1)
    observer.memory_job_schedule_observed("disabled")
    observer.rewrite_observed("ok", 0.1)
    observer.degraded("correlation", "operation", "Error", "fallback")
    observer.conversation_write_observed("stored")


class _RecordingStore:
    def __init__(self) -> None:
        self.schedule_flags: list[bool] = []
        self.last_reference: CompletedTurnReference | None = None
        self.created_sessions: list[str] = []
        self.queued: list[tuple[UUID, CompletedTurnReference]] = []
        self.appended: list[tuple[object, object]] = []
        self.source_conversations: dict[str, UUID] = {}
        self.source_owners: dict[str, str] = {}
        self.deletion_pending: set[tuple[str, str]] = set()
        self.deleted: list[tuple[str, str]] = []
        self.formed_memory = None
        self.source_references = {}

    async def create_conversation(self, user_id, *, title=None):
        del title
        session_id = f"managed-session-{len(self.created_sessions) + 1}"
        self.created_sessions.append(session_id)
        self.source_owners[session_id] = user_id
        self.source_conversations[session_id] = UUID(int=1000 + len(self.created_sessions))
        return SimpleNamespace(
            session_id=session_id, conversation_id=self.source_conversations[session_id]
        )

    async def is_conversation_active(self, user_id, session_id):
        del user_id, session_id
        return True

    async def active_conversation_id(self, user_id, session_id):
        del user_id
        return self.source_conversations.get(session_id, UUID(int=1))

    async def read_recent(self, user_id, session_id, limit):
        del user_id, session_id, limit
        return ()

    async def append_turn(self, user_id, user_message, assistant_message, *, schedule_memory=False):
        self.schedule_flags.append(schedule_memory)
        self.appended.append((user_message, assistant_message))
        self.source_owners[user_message.session_id] = user_id
        conversation_id = self.source_conversations.setdefault(
            user_message.session_id, UUID(int=len(self.source_conversations) + 1)
        )
        result = AppendTurnResult(
            inserted=True,
            reference=CompletedTurnReference(
                user_id=user_id,
                session_id=user_message.session_id,
                conversation_id=conversation_id,
                turn_id=user_message.turn_id,
                boundary_message_id=len(self.schedule_flags) * 2,
            ),
            memory_job_event_id=UUID(int=299 + len(self.schedule_flags))
            if schedule_memory
            else None,
        )
        self.last_reference = result.reference
        if result.memory_job_event_id:
            self.source_references[result.memory_job_event_id] = result.reference
            self.queued.append((result.memory_job_event_id, result.reference))
        return result

    async def mark_deletion_pending(self, user_id, session_id):
        if self.source_owners.get(session_id) != user_id:
            return False
        self.deletion_pending.add((user_id, session_id))
        return True

    async def purge_deletion_pending(self, user_id, session_id):
        if (user_id, session_id) not in self.deletion_pending:
            return False
        self.queued[:] = [
            (event_id, reference)
            for event_id, reference in self.queued
            if (reference.user_id, reference.session_id) != (user_id, session_id)
        ]
        if self.formed_memory is not None:
            for event_id, (source, _result) in tuple(self.formed_memory.results.items()):
                if (source.reference.user_id, source.reference.session_id) == (user_id, session_id):
                    del self.formed_memory.results[event_id]
        del self.source_owners[session_id]
        self.deletion_pending.remove((user_id, session_id))
        self.deleted.append((user_id, session_id))
        return True


class _PersistentEvaluator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def evaluate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            persistence=SimpleNamespace(memories=(SimpleNamespace(memory_id=UUID(int=200)),))
        )


async def test_persistent_formation_registers_ownership_before_and_after_write(tmp_path: Path):
    case = _formation_case()
    artifacts, plan = _isolated_store(tmp_path, case)
    store = _RecordingStore()
    evaluator = _PersistentEvaluator()
    runtime = PersistentFormationRuntime(
        plan=plan,
        ledger=artifacts.load_isolation_ledger(),
        artifacts=artifacts,
        conversation_store=store,  # type: ignore[arg-type]
        evaluator=evaluator,  # type: ignore[arg-type]
    )

    result = await runtime.evaluate(case)

    assert result.persistence.memories[0].memory_id == UUID(int=200)
    assert store.schedule_flags == [False]
    owned = artifacts.load_isolation_ledger().resources[0]
    assert owned.user_id.startswith(plan.user_namespace)
    assert owned.conversation_id == UUID(int=1)
    assert owned.memory_ids == (UUID(int=200),)
    assert evaluator.calls[0]["reference"].conversation_id == UUID(int=1)


class _Queue:
    def __init__(self, store: _RecordingStore) -> None:
        self.store = store

    async def claim_due(self, **kwargs):
        assert kwargs["limit"] == 1
        event_id, reference = self.store.queued.pop(0)
        return (
            MemoryJob(
                event_id=event_id,
                reference=reference,
                attempt_count=1,
                lease_token=UUID(int=301),
                lease_expires_at=datetime(2026, 9, 19, 1, tzinfo=UTC),
            ),
        )


@pytest.mark.parametrize("cancelled", (False, True))
async def test_source_queue_filters_native_claim_and_resets_target(monkeypatch, cancelled):
    queue = _SourceMemoryJobQueue(SimpleNamespace())
    expected = UUID(int=300)
    queries = []

    async def capture_claim(self, **kwargs):
        statement = self._claim_candidates(kwargs["limit"], kwargs["max_attempts"])
        assert statement._where_criteria[-1].compare(memory_jobs.c.event_id == expected)
        compiled = statement.compile(dialect=postgresql.dialect())
        assert expected in compiled.params.values()
        assert "memory_jobs.event_id = " in str(compiled)
        assert "FOR UPDATE OF memory_jobs SKIP LOCKED" in str(compiled)
        queries.append(compiled)
        if cancelled:
            raise asyncio.CancelledError
        return ()

    monkeypatch.setattr(PostgresMemoryJobQueueAdapter, "claim_due", capture_claim)
    with pytest.raises(CrossSessionProtocolError):
        queue._claim_candidates(1, 2)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await queue.claim_expected(expected, limit=1, max_attempts=2)
    else:
        assert await queue.claim_expected(expected, limit=1, max_attempts=2) == ()
    assert len(queries) == 1
    with pytest.raises(CrossSessionProtocolError):
        queue._claim_candidates(1, 2)


class _JobProcessor:
    async def execute(self, job):
        assert job.event_id.int >= 300
        return ProcessMemoryJobResult(MemoryJobProcessOutcome.COMPLETED, 1)


class _Inspector:
    def __init__(self, store):
        self.store = store

    async def inspect(self, *, event_id, user_id):
        reference = self.store.source_references[event_id]
        lifecycle = FormationLifecycleRecord(
            event="ADD",
            memory_id=UUID(int=400),
            memory="Ưu tiên Hà Nội",
        )
        return FormationPersistenceSnapshot(
            event_id=event_id,
            user_id=user_id,
            memories=(
                PersistedFormationMemory(
                    memory_id=UUID(int=400),
                    content="Ưu tiên Hà Nội",
                    user_id=user_id,
                    formation_event_id=event_id,
                    conversation_id=UUID(int=1),
                    turn_id=reference.turn_id,
                    boundary_message_id=reference.boundary_message_id,
                    attributed_to="user",
                ),
            ),
            receipt=FormationReceiptRecord(
                event_id=event_id,
                user_id=user_id,
                conversation_id=UUID(int=1),
                events=(lifecycle,),
                memory_count=1,
                committed_at=datetime(2026, 9, 19, tzinfo=UTC),
            ),
        )


class _NeverFormationJudge:
    async def formation(self, **kwargs):
        raise AssertionError(f"exact mapping must not call judge: {kwargs}")


async def test_cross_session_runtime_owns_job_and_disables_session_b_formation(tmp_path: Path):
    case = _cross_case()
    artifacts, plan = _isolated_store(tmp_path, case)
    store = _RecordingStore()
    memory = _Memory()
    backend_kira = _Kira()
    runtime = NativeCrossSessionRuntime(
        plan=plan,
        ledger=artifacts.load_isolation_ledger(),
        artifacts=artifacts,
        settings=_base_settings(),
        conversation_store=store,  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        memory_queue=_Queue(store),  # type: ignore[arg-type]
        process_job=_JobProcessor(),  # type: ignore[arg-type]
        inspector=_Inspector(store),  # type: ignore[arg-type]
        rewriter=_Rewriter(),  # type: ignore[arg-type]
        kira_factory=lambda _username: backend_kira,  # type: ignore[return-value]
        judge=_NeverFormationJudge(),  # type: ignore[arg-type]
        gold_fact_text={"conv01:M01": "Ưu tiên Hà Nội"},
    )

    handle = await runtime.persist_session_a(case)
    readiness = await runtime.wait_for_memory(handle, timeout_seconds=1)

    assert handle.event_id == UUID(int=300)
    assert readiness.status is MemoryReadinessStatus.COMPLETED
    assert readiness.gold_ids_by_memory_id == {
        "00000000-0000-0000-0000-000000000190": ("conv01:M01",)
    }
    no_ltm = await runtime.run_session_b(
        case,
        condition=CrossSessionCondition.NO_LTM,
        enable_ltm=False,
        schedule_memory=False,
    )
    with_ltm = await runtime.run_session_b(
        case,
        condition=CrossSessionCondition.WITH_LTM,
        enable_ltm=True,
        schedule_memory=False,
    )

    assert no_ltm.retrieved_memories == ()
    assert with_ltm.retrieved_memories[0].memory_id == "memory-1"
    assert no_ltm.final_answer == "Tôi ưu tiên ở đâu?"
    assert with_ltm.final_answer.startswith("rewritten:ConversationContext")
    assert store.schedule_flags == [True, False, False]
    assert len(set(store.created_sessions)) == 2
    assert artifacts.load_isolation_ledger().resources[0].memory_ids == (UUID(int=400),)

    with pytest.raises(CrossSessionProtocolError):
        await runtime.run_session_b(
            case,
            condition=CrossSessionCondition.WITH_LTM,
            enable_ltm=True,
            schedule_memory=True,
        )


class _BoundaryStore(_RecordingStore):
    async def read_through_boundary(self, user_id, conversation_id, boundary_message_id, limit):
        del user_id
        selected = [
            message
            for index, pair in enumerate(self.appended)
            if (index + 1) * 2 <= boundary_message_id
            and self.source_conversations[pair[0].session_id] == conversation_id
            for message in pair
        ]
        return tuple(selected[-limit:])


class _BoundaryMemory(_Memory):
    def __init__(self):
        super().__init__()
        self.results = {}

    async def process_memory(self, source):
        self.processed.append(source)
        # One additive assertion per source event, deliberately keeping repeated text.
        result = MemoryProcessResult(
            events=(
                MemoryLifecycleEvent(
                    action="ADD",
                    memory_id=str(UUID(int=source.formation_event_id.int + 100)),
                    content=source.messages[-2].content,
                ),
            )
        )
        self.results[source.formation_event_id] = (source, result)
        return result


class _ReceiptQueue(_Queue):
    def __init__(self, store):
        super().__init__(store)
        self.completed = []
        self.timeline = []

    async def claim_due(self, **kwargs):
        result = await super().claim_due(**kwargs)
        self.timeline.append(("claim", result[0].event_id))
        return result

    async def complete(self, event_id, lease_token, *, lifecycle_event_count):
        del lease_token, lifecycle_event_count
        self.completed.append(event_id)
        self.timeline.append(("complete", event_id))


class _ReceiptInspector:
    def __init__(self, memory, queue):
        self.memory = memory
        self.queue = queue

    async def inspect(self, *, event_id, user_id):
        if event_id not in self.queue.completed:
            return FormationPersistenceSnapshot(
                event_id=event_id, user_id=user_id, memories=(), receipt=None
            )
        source, result = self.memory.results[event_id]
        ref = source.reference
        return FormationPersistenceSnapshot(
            event_id=event_id,
            user_id=user_id,
            memories=tuple(
                PersistedFormationMemory(
                    memory_id=UUID(event.memory_id),
                    content=event.content,
                    user_id=user_id,
                    formation_event_id=event_id,
                    conversation_id=ref.conversation_id,
                    turn_id=ref.turn_id,
                    boundary_message_id=ref.boundary_message_id,
                )
                for event in result.events
            ),
            receipt=FormationReceiptRecord(
                event_id=event_id,
                user_id=user_id,
                conversation_id=ref.conversation_id,
                events=tuple(
                    FormationLifecycleRecord(
                        event="ADD", memory_id=UUID(event.memory_id), memory=event.content
                    )
                    for event in result.events
                ),
                memory_count=len(result.events),
                committed_at=datetime.now(UTC),
            ),
        )


@pytest.mark.parametrize(
    "delivery",
    (
        "ordered",
        "reversed",
        "dead",
        "missing_receipt",
        "timeout",
        "outer_timeout",
        "legacy_dead",
        "cleanup_failure",
        "persist_failure",
    ),
)
async def test_source_trajectory_uses_all_receipts_and_preserves_context_boundaries(
    tmp_path, delivery
):
    case = _cross_case()
    base = datetime(2026, 10, 3, tzinfo=UTC)
    messages = tuple(
        Message(
            message_id=f"turn-{index}-{role}",
            session_id=session,
            role=role,
            content=text if role == "user" else "Đã ghi nhận",
            timestamp=base + timedelta(seconds=index, microseconds=offset),
        )
        for index, (session, text) in enumerate(
            (("source-a", "98"), ("source-a", "99"), ("source-b", "98")), start=1
        )
        for offset, role in enumerate(("user", "assistant"))
    )
    case = case.model_copy(
        update={"inputs": case.inputs.model_copy(update={"session_a_messages": messages})}
    )
    artifacts, plan = _isolated_store(
        tmp_path,
        case,
        additional_case_ids=("conv01:cross-session:Q02", "conv02:cross-session:Q01"),
    )

    class SourceStore(_BoundaryStore):
        def __init__(self):
            super().__init__()
            self.persist_fault_used = False

        async def append_turn(self, *args, **kwargs):
            if (
                delivery == "persist_failure"
                and len(self.appended) == 1
                and not self.persist_fault_used
            ):
                self.persist_fault_used = True
                raise OSError("test append failure")
            return await super().append_turn(*args, **kwargs)

        async def mark_deletion_pending(self, user_id, session_id):
            if delivery == "cleanup_failure":
                raise OSError("test cleanup failure")
            return await super().mark_deletion_pending(user_id, session_id)

    store = SourceStore()
    memory = _BoundaryMemory()
    store.formed_memory = memory
    foreign_source = SimpleNamespace(
        reference=SimpleNamespace(user_id="foreign-user", session_id="foreign-session")
    )
    store.source_owners["foreign-session"] = "foreign-user"
    memory.results[UUID(int=777)] = (foreign_source, MemoryProcessResult())
    queue = _ReceiptQueue(store)
    processor = ProcessMemoryJobUseCase(
        ProcessMemoryUseCase(store, memory), queue, max_attempts=1, retry_delays_seconds=()
    )
    inspector = _ReceiptInspector(memory, queue)

    class DeliveredProcessor:
        async def execute(self, job):
            if delivery in {"dead", "legacy_dead", "cleanup_failure"} and job.event_id == UUID(
                int=301
            ):
                return ProcessMemoryJobResult(MemoryJobProcessOutcome.DEAD)
            if delivery in {"timeout", "outer_timeout"} and job.event_id == UUID(int=301):
                await asyncio.sleep(10)
            return await processor.execute(job)

    class DeliveredInspector:
        async def inspect(self, *, event_id, user_id):
            snapshot = await inspector.inspect(event_id=event_id, user_id=user_id)
            if delivery == "missing_receipt" and event_id == UUID(int=301):
                return snapshot.model_copy(update={"receipt": None})
            return snapshot

    native_mark = store.mark_deletion_pending
    native_purge = store.purge_deletion_pending
    legacy_cleanup_calls = []

    async def legacy_cleanup(user_id, session_ids):
        legacy_cleanup_calls.append((user_id, session_ids))
        for session_id in session_ids:
            await native_mark(user_id, session_id)
        for session_id in session_ids:
            await native_purge(user_id, session_id)

    if delivery == "legacy_dead":
        store.mark_deletion_pending = None
        store.purge_deletion_pending = None

    runtime = NativeCrossSessionRuntime(
        plan=plan,
        ledger=artifacts.load_isolation_ledger(),
        artifacts=artifacts,
        settings=_base_settings(),
        conversation_store=store,
        memory=memory,
        memory_queue=queue,
        process_job=DeliveredProcessor(),
        inspector=DeliveredInspector(),
        rewriter=_Rewriter(),
        kira_factory=lambda _: _Kira(),
        judge=_NeverFormationJudge(),
        gold_fact_text={"conv01:M01": "98"},
        gold_fact_source_ids={"conv01:M01": ("turn-3-user",)},
        failed_source_cleanup=legacy_cleanup if delivery == "legacy_dead" else None,
    )

    if delivery == "persist_failure":
        with pytest.raises(OSError, match="append failure"):
            await runtime.persist_session_a(case)
        handle = None
        assert not store.queued
        assert set(memory.results) == {UUID(int=777)}
    else:
        handle = await runtime.persist_session_a(case)
        assert len(handle.source_event_ids) == 3
    assert not queue.timeline  # No leases expire while later source turns are being persisted.

    async def next_case_survives_without_deleting_another_owner():
        next_case = case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
        # A failed source remains failed for every QA in its own bundle. Another
        # bundle/user can proceed without replaying or deleting the failed user's source.
        with pytest.raises(CrossSessionDependencyError):
            await runtime.persist_session_a(next_case)
        next_case = next_case.model_copy(
            update={
                "case_id": "conv02:cross-session:Q01",
                "inputs": next_case.inputs.model_copy(
                    update={"user_id": "conv02:user", "session_a": "conv02:source"}
                ),
            }
        )
        next_handle = await runtime.persist_session_a(next_case)
        next_readiness = await runtime.wait_for_memory(next_handle, timeout_seconds=2)
        assert next_readiness.status is MemoryReadinessStatus.COMPLETED
        assert not store.queued
        assert store.source_owners["foreign-session"] == "foreign-user"
        assert memory.results[UUID(int=777)][0] is foreign_source
        assert all(user_id != "foreign-user" for user_id, _session in store.deleted)

    if delivery == "persist_failure":
        await next_case_survives_without_deleting_another_owner()
        return
    if delivery == "cleanup_failure":
        with pytest.raises(CrossSessionDependencyError):
            await runtime.wait_for_memory(handle, timeout_seconds=2)
        before = tuple(queue.timeline)
        with pytest.raises(CrossSessionDependencyError):
            await runtime.persist_session_a(
                case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
            )
        assert tuple(queue.timeline) == before
        assert len(store.queued) == 1
        return

    if delivery == "reversed":
        store.queued.reverse()
        with pytest.raises(CrossSessionProtocolError):
            await runtime.wait_for_memory(handle, timeout_seconds=2)
        assert not store.queued
        assert set(memory.results) == {UUID(int=777)}
        await next_case_survives_without_deleting_another_owner()
        return
    if delivery == "missing_receipt":
        with pytest.raises(CrossSessionProtocolError):
            await runtime.wait_for_memory(handle, timeout_seconds=2)
        assert not store.queued
        assert set(memory.results) == {UUID(int=777)}
        await next_case_survives_without_deleting_another_owner()
        return
    if delivery == "timeout":
        with pytest.raises(TimeoutError):
            await runtime.wait_for_memory(handle, timeout_seconds=0.01)
        assert not store.queued
        assert set(memory.results) == {UUID(int=777)}
        await next_case_survives_without_deleting_another_owner()
        return
    if delivery == "outer_timeout":
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await runtime.wait_for_memory(handle, timeout_seconds=2)
        assert store.queued
        assert len(memory.processed) == 1
        assert not artifacts.load_bundle_source("conv01").failed
        assert not artifacts.load_bundle_source("conv01").ready
        return
    readiness = await runtime.wait_for_memory(handle, timeout_seconds=2)
    if delivery in {"dead", "legacy_dead"}:
        assert readiness.status is MemoryReadinessStatus.DEAD
        assert not readiness.gold_ids_by_memory_id
        assert not store.queued
        assert set(memory.results) == {UUID(int=777)}
        await next_case_survives_without_deleting_another_owner()
        assert bool(legacy_cleanup_calls) is (delivery == "legacy_dead")
        return

    delivered_ids = (
        tuple(reversed(handle.source_event_ids))
        if delivery == "reversed"
        else handle.source_event_ids
    )
    assert queue.timeline == [
        entry
        for event_id in delivered_ids
        for entry in (("claim", event_id), ("complete", event_id))
    ]
    by_event = {source.formation_event_id: source for source in memory.processed}
    assert [
        tuple(message.content for message in by_event[event_id].messages[::2])
        for event_id in handle.source_event_ids
    ] == [("98",), ("98", "99"), ("98", "99", "98")]
    assert len({source.reference.conversation_id for source in memory.processed}) == 1
    assert [by_event[event_id].messages[-2].timestamp for event_id in handle.source_event_ids] == [
        messages[0].timestamp,
        messages[2].timestamp,
        messages[4].timestamp,
    ]
    assert len(artifacts.load_isolation_ledger().resources[0].memory_ids) == 3
    assert readiness.gold_ids_by_memory_id == {
        str(UUID(int=400)): (),
        str(UUID(int=401)): (),
        str(UUID(int=402)): ("conv01:M01",),
    }


async def test_kira_history_is_independent_for_each_case_arm_and_attempt(tmp_path, monkeypatch):
    case = _cross_case()
    artifacts, plan = _isolated_store(tmp_path, case)
    store = _RecordingStore()
    history = {}
    issued = []

    class HistoryKira(_Kira):
        def __init__(self, username):
            super().__init__()
            self.username = username

        async def chat_stream(self, message):
            previous = history.setdefault(self.username, [])
            output = f"history={len(previous)};{message}"
            previous.append(message)
            return await super().chat_stream(output)

    def factory(username):
        issued.append(username)
        return HistoryKira(username)

    runtime = NativeCrossSessionRuntime(
        plan=plan,
        ledger=artifacts.load_isolation_ledger(),
        artifacts=artifacts,
        settings=_base_settings(),
        conversation_store=store,
        memory=_Memory(),
        memory_queue=_Queue(store),
        process_job=_JobProcessor(),
        inspector=_Inspector(store),
        rewriter=_Rewriter(),
        kira_factory=factory,
        judge=_NeverFormationJudge(),
        gold_fact_text={"conv01:M01": "Ưu tiên Hà Nội"},
    )
    handle = await runtime.persist_session_a(case)
    await runtime.wait_for_memory(handle, timeout_seconds=1)
    outputs = [
        await runtime.run_session_b(case, condition=arm, enable_ltm=enabled, schedule_memory=False)
        for arm, enabled in (
            (CrossSessionCondition.WITH_LTM, True),
            (CrossSessionCondition.NO_LTM, False),
        )
    ]
    assert all(output.final_answer.startswith("history=0;") for output in outputs)
    assert len(set(issued)) == 2
    assert outputs[0].kira_context_identity_sha256 != outputs[1].kira_context_identity_sha256
    assert all(len(username) == 38 and username.startswith("bench_") for username in issued)
    for case_id, attempt in ((case.case_id, 2), ("conv01:cross-session:Q02", 1)):
        next_case = case.model_copy(update={"case_id": case_id})
        runtime.ledger = register_case_resources(
            runtime.ledger,
            plan,
            allocate_bundle_qa_resources(
                plan, bundle_id="conv01", case_id=case_id, attempt=attempt
            ),
        )
        monkeypatch.setattr(artifacts, "next_attempt_number", lambda _case_id, n=attempt: n)
        for arm in (CrossSessionCondition.NO_LTM, CrossSessionCondition.WITH_LTM):
            execution = await runtime.run_session_b(
                next_case,
                condition=arm,
                enable_ltm=arm is CrossSessionCondition.WITH_LTM,
                schedule_memory=False,
            )
            assert execution.final_answer.startswith("history=0;")
    assert len(set(issued)) == 6


async def test_cross_session_runtime_blocks_unapproved_kira_mode_before_db_mutation(
    tmp_path, monkeypatch
):
    case = _cross_case()
    artifacts, plan = _isolated_store(tmp_path, case)

    async def unexpected_reset(*_args):
        raise AssertionError("unapproved runtime must not reset benchmark state")

    monkeypatch.setattr("evaluation.native_runtime._initialize_owned_state", unexpected_reset)
    config = _native_config().model_copy(update={"suites": (Suite.CROSS_SESSION,)})
    with pytest.raises(ValueError, match="unique_username"):
        await create_native_runtime(
            config=config,
            base_settings=_base_settings(),
            plan=plan,
            ledger=artifacts.load_isolation_ledger(),
            artifacts=artifacts,
            cases=(case,),
        )


async def test_persist_message_pairs_schedules_only_the_final_boundary():
    store = _RecordingStore()
    messages = (
        Message(message_id="turn-1", role="user", content="u1"),
        Message(message_id="turn-1-a", role="assistant", content="a1"),
        Message(message_id="turn-2", role="user", content="u2"),
        Message(message_id="turn-2-a", role="assistant", content="a2"),
    )

    reference = await _persist_message_pairs(  # type: ignore[arg-type]
        store,
        user_id="eval:user",
        session_id="eval:session",
        messages=messages,
        schedule_last=True,
    )

    assert store.schedule_flags == [False, True]
    assert reference.turn_id == _owned_turn_id("eval:user", "eval:session", "turn-2")


async def test_persist_message_pairs_rejects_incomplete_misordered_or_existing_state():
    store = _RecordingStore()
    with pytest.raises(ValueError, match="complete"):
        await _persist_message_pairs(  # type: ignore[arg-type]
            store,
            user_id="eval:user",
            session_id="eval:session",
            messages=(),
            schedule_last=False,
        )
    with pytest.raises(ValueError, match="ordered"):
        await _persist_message_pairs(  # type: ignore[arg-type]
            store,
            user_id="eval:user",
            session_id="eval:session",
            messages=(
                Message(message_id="turn-1", role="assistant", content="a"),
                Message(message_id="turn-1-a", role="user", content="u"),
            ),
            schedule_last=False,
        )

    class ExistingStore(_RecordingStore):
        async def append_turn(self, *args, **kwargs):
            inserted = await super().append_turn(*args, **kwargs)
            return AppendTurnResult(
                inserted=False,
                reference=inserted.reference,
                memory_job_event_id=inserted.memory_job_event_id,
            )

    with pytest.raises(ValueError, match="not fresh"):
        await _persist_message_pairs(  # type: ignore[arg-type]
            ExistingStore(),
            user_id="eval:user",
            session_id="eval:session",
            messages=(
                Message(message_id="turn-1", role="user", content="u"),
                Message(message_id="turn-1-a", role="assistant", content="a"),
            ),
            schedule_last=False,
        )


class _FixtureManager:
    def __init__(self, fixture: str) -> None:
        self.fixture = fixture
        self.setup_calls: list[object] = []
        self.cleaned: list[object] = []

    async def setup(self, *values):
        self.setup_calls.append(values)
        return self.fixture

    async def cleanup(self, fixture):
        self.cleaned.append(fixture)


class _RetrievalEvaluator:
    async def evaluate_gold_fixture(self, case, fixture):
        return (case.case_id, fixture)

    async def evaluate_formation_produced(self, case, fixture):
        return (case.case_id, fixture)


class _CorpusBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build_or_load_corpus(self):
        self.calls += 1
        return "corpus"


async def test_retrieval_runtime_sets_up_once_and_cleans_exact_fixtures(tmp_path: Path):
    case = _formation_case()
    _, plan = _isolated_store(tmp_path, case)
    formation = _CorpusBuilder()
    runtime = PersistentRetrievalRuntime(
        plan=plan,
        cases=(),
        client=SimpleNamespace(),  # type: ignore[arg-type]
        evaluator=_RetrievalEvaluator(),  # type: ignore[arg-type]
        formation=formation,  # type: ignore[arg-type]
    )
    gold = _FixtureManager("gold")
    formed = _FixtureManager("formed")
    runtime._gold_manager = gold  # type: ignore[assignment]
    runtime._formed_manager = formed  # type: ignore[assignment]

    assert await runtime.evaluate_gold(case) == (case.case_id, "gold")
    assert await runtime.evaluate_formed(case) == (case.case_id, "formed")
    assert formation.calls == 1
    assert len(gold.setup_calls) == len(formed.setup_calls) == 1

    await runtime.cleanup()
    assert gold.cleaned == ["gold"]
    assert formed.cleaned == ["formed"]
    await runtime.cleanup()


async def test_retrieval_runtime_rolls_back_partial_setup_and_reports_cleanup_failure(
    tmp_path: Path,
):
    case = _formation_case()
    _, plan = _isolated_store(tmp_path, case)
    runtime = PersistentRetrievalRuntime(
        plan=plan,
        cases=(),
        client=SimpleNamespace(),  # type: ignore[arg-type]
        evaluator=_RetrievalEvaluator(),  # type: ignore[arg-type]
        formation=_CorpusBuilder(),  # type: ignore[arg-type]
    )
    gold = _FixtureManager("gold")

    class BrokenSetup(_FixtureManager):
        async def setup(self, *values):
            self.setup_calls.append(values)
            raise RuntimeError("fixture setup failed")

    runtime._gold_manager = gold  # type: ignore[assignment]
    runtime._formed_manager = BrokenSetup("formed")  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="fixture setup failed"):
        await runtime.evaluate_gold(case)
    assert gold.cleaned == ["gold"]

    class BrokenCleanup(_FixtureManager):
        async def cleanup(self, fixture):
            self.cleaned.append(fixture)
            raise RuntimeError("sensitive cleanup detail")

    runtime._gold_manager = BrokenCleanup("gold")  # type: ignore[assignment]
    runtime._formed_manager = BrokenCleanup("formed")  # type: ignore[assignment]
    runtime._gold = "gold"  # type: ignore[assignment]
    runtime._formed = "formed"  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="retrieval_fixture_cleanup_failed"):
        await runtime.cleanup()


class _ClosableClient:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _DisposableEngine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


async def test_runtime_resources_close_all_owned_clients_once():
    memory = _Memory()
    first = _ClosableClient()
    second = _ClosableClient()
    engine = _DisposableEngine()
    resources = NativeRuntimeResources(
        settings=_base_settings(),
        engine=engine,  # type: ignore[arg-type]
        http_clients=(first, second),  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        formation=SimpleNamespace(),  # type: ignore[arg-type]
        retrieval=SimpleNamespace(),  # type: ignore[arg-type]
        cross_session=SimpleNamespace(),  # type: ignore[arg-type]
        rewriter=SimpleNamespace(),  # type: ignore[arg-type]
        judge=SimpleNamespace(),  # type: ignore[arg-type]
    )

    await resources.aclose()
    await resources.aclose()

    assert memory.closed == 1
    assert first.closed == second.closed == engine.disposed == 1


def test_interrupted_owned_attempt_is_closed_before_resume(tmp_path: Path):
    plan = create_isolation_plan(
        run_id=_RUN_ID,
        owner_token=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        conversation_database_url="postgresql://eval:secret@localhost/eval",
        memory_database_url="postgresql://eval:secret@localhost/eval",
    )
    config = load_config(profile=Profile.MOCK, suites=(Suite.REWRITE,))
    preparation, _ = prepare_benchmark_run(
        run_id=_RUN_ID,
        config=config,
        provenance=_provenance(),
        dataset_root=default_dataset_root(),
        seed=742,
        isolation_sha256=isolation_plan_sha256(plan),
    )
    store: ArtifactStore = create_or_resume_store(
        tmp_path / "run",
        preparation,
        now=datetime(2026, 9, 19, tzinfo=UTC),
    )
    store.write_isolation_plan(plan)
    case = preparation.selected_cases[0]
    ledger = register_case_resources(
        new_isolation_ledger(plan),
        plan,
        allocate_case_resources(plan, case_id=case.case_id, attempt=1),
    )
    store.write_isolation_ledger(ledger)

    reconcile_interrupted_attempts(store, preparation.selected_cases)

    attempt = store.latest_attempts[0]
    assert attempt.outcome is Outcome.PROTOCOL_ERROR
    assert attempt.reason_codes == ("benchmark_attempt_interrupted",)
    assert store.next_attempt_number(case.case_id) == 2
    assert case.case_id in store.resume_plan().run_case_ids


class _SharedScopedMemory(_BoundaryMemory):
    def __init__(self):
        super().__init__()
        self.searches = []

    async def search_scoped(self, user_id, query, *, conversation_id, scope, top_k, threshold):
        self.searches.append((user_id, query, conversation_id, scope))
        owned = [
            (source, result)
            for source, result in self.results.values()
            if source.reference.user_id == user_id
        ]
        if not owned:
            return ()
        source, result = owned[-1]
        if scope == "conversation" and conversation_id != source.reference.conversation_id:
            return ()
        event = result.events[0]
        return (
            LongTermMemory(
                event.memory_id,
                event.content,
                0.9,
                {
                    "user_id": user_id,
                    "conversation_id": str(source.reference.conversation_id),
                    "memory_scope": "GLOBAL" if scope == "global" else "CONVERSATION",
                    "source_timestamp": source.messages[-2].timestamp.isoformat(),
                },
            ),
        )


class _CleanContextRewriter:
    def __init__(self):
        self.contexts = []

    async def rewrite(self, context):
        self.contexts.append(context)
        assert context.recent_messages == ()
        assert all(item.metadata["memory_scope"] == "GLOBAL" for item in context.long_term_memories)
        return context.current_query


def _shared_runtime(artifacts, plan, store, memory, queue, *, rewriter=None, kira_factory=None):
    processor = ProcessMemoryJobUseCase(
        ProcessMemoryUseCase(store, memory), queue, max_attempts=1, retry_delays_seconds=()
    )
    return NativeCrossSessionRuntime(
        plan=plan,
        ledger=artifacts.load_isolation_ledger(),
        artifacts=artifacts,
        settings=_base_settings(),
        conversation_store=store,
        memory=memory,
        memory_queue=queue,
        process_job=processor,
        inspector=_ReceiptInspector(memory, queue),
        rewriter=rewriter or _Rewriter(),
        kira_factory=kira_factory or (lambda _: _Kira()),
        judge=_NeverFormationJudge(),
        gold_fact_text={},
    )


async def test_full_dataset_source_once_four_users_and_clean_product_qa(tmp_path):
    cases = sorted(
        (case for case in compile_dataset(seed=742).cases if case.suite is Suite.CROSS_SESSION),
        key=lambda case: case.case_id.split(":")[0],
    )
    artifacts, plan = _isolated_store(
        tmp_path, cases[0], additional_case_ids=tuple(case.case_id for case in cases[1:])
    )
    store = _BoundaryStore()
    memory = _SharedScopedMemory()
    queue = _ReceiptQueue(store)
    rewriter = _CleanContextRewriter()
    usernames = []

    def kira(username):
        usernames.append(username)
        return _Kira()

    runtime = _shared_runtime(
        artifacts, plan, store, memory, queue, rewriter=rewriter, kira_factory=kira
    )
    formed_per_bundle = {}
    for case in cases:
        bundle = case.case_id.split(":")[0]
        handle = await runtime.persist_session_a(case)
        ready = await runtime.wait_for_memory(handle, timeout_seconds=30)
        assert ready.status is MemoryReadinessStatus.COMPLETED
        formed_per_bundle[bundle] = len(memory.processed)
        source_before = tuple(memory.results)
        for arm, enabled in (
            (CrossSessionCondition.NO_LTM, False),
            (CrossSessionCondition.WITH_LTM, True),
        ):
            output = await runtime.run_session_b(
                case, condition=arm, enable_ltm=enabled, schedule_memory=False
            )
            assert bool(output.retrieved_memories) is enabled
        assert tuple(memory.results) == source_before
    assert formed_per_bundle == {"conv01": 87, "conv02": 145, "conv03": 202, "conv04": 253}
    assert len(memory.processed) == len(queue.completed) == 253
    assert len({source.reference.conversation_id for source in memory.processed}) == 4
    assert len({source.reference.user_id for source in memory.processed}) == 4
    assert len(store.created_sessions) == len(set(store.created_sessions)) == 418
    assert len(usernames) == len(set(usernames)) == 418
    assert len(rewriter.contexts) == 209
    assert sum(store.schedule_flags) == 253
    ledger = artifacts.load_isolation_ledger()
    owners = [item for item in ledger.resources if item.resource_role == "bundle_source"]
    assert len(owners) == 4
    assert len({item.user_id for item in owners}) == 4
    assert (
        len(
            {
                item.memory_id
                for source in memory.processed
                for item in memory.results[source.formation_event_id][1].events
            }
        )
        == 253
    )
    for bundle in ("conv01", "conv02", "conv03", "conv04"):
        corpus = artifacts.load_bundle_source(bundle)
        assert corpus.ready
        assert len({event.conversation_id for event in corpus.events}) == 1
    for context in rewriter.contexts:
        assert len({memory.metadata["user_id"] for memory in context.long_term_memories}) == 1


async def test_shared_source_survives_qa_retry_and_process_resume_without_reextraction(tmp_path):
    case = _cross_case()
    second = case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
    artifacts, plan = _isolated_store(tmp_path, case, additional_case_ids=(second.case_id,))
    store = _BoundaryStore()
    memory = _SharedScopedMemory()
    queue = _ReceiptQueue(store)
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    handle = await runtime.persist_session_a(case)
    ready = await runtime.wait_for_memory(handle, timeout_seconds=2)
    assert len(memory.processed) == 1
    before = artifacts.load_bundle_source("conv01")
    # New process: no in-memory source cache, same durable run/corpus.
    resumed_store = ArtifactStore.resume(
        artifacts.root, expected_identity=artifacts.manifest.identity
    )
    runtime = _shared_runtime(resumed_store, plan, store, memory, queue)
    handle = await runtime.persist_session_a(second)
    recovered = await runtime.wait_for_memory(handle, timeout_seconds=2)
    assert recovered.gold_ids_by_memory_id == ready.gold_ids_by_memory_id
    assert len(memory.processed) == 1
    assert artifacts.load_bundle_source("conv01") == before
    # A new QA attempt owns only its fresh context; source memory ownership stays put.
    original_attempt = artifacts.next_attempt_number
    artifacts.next_attempt_number = lambda _: 2
    runtime.artifacts = artifacts
    handle = await runtime.persist_session_a(second)
    await runtime.wait_for_memory(handle, timeout_seconds=2)
    output = await runtime.run_session_b(
        second, condition=CrossSessionCondition.WITH_LTM, enable_ltm=True, schedule_memory=False
    )
    assert output.retrieved_memories
    assert len(memory.processed) == 1
    assert artifacts.load_bundle_source("conv01") == before
    artifacts.next_attempt_number = original_attempt


async def test_source_transient_retry_reuses_event_and_corpus_for_later_qa(tmp_path):
    case = _cross_case()
    second = case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
    artifacts, plan = _isolated_store(tmp_path, case, additional_case_ids=(second.case_id,))
    store = _BoundaryStore()

    class TransientMemory(_SharedScopedMemory):
        def __init__(self):
            super().__init__()
            self.attempted = []

        async def process_memory(self, source):
            self.attempted.append(source.formation_event_id)
            if len(self.attempted) == 1:
                raise LongTermMemoryConnectionError
            return await super().process_memory(source)

    class RetryQueue(_ReceiptQueue):
        def __init__(self, store):
            super().__init__(store)
            self.deliveries = []
            self.next_attempt_at = None
            self.not_due_claims = 0

        async def claim_expected(self, event_id, **kwargs):
            assert kwargs["max_attempts"] == 2
            if self.next_attempt_at is not None and datetime.now(UTC) < self.next_attempt_at:
                self.not_due_claims += 1
                return ()
            jobs = await super().claim_due(**kwargs)
            assert jobs[0].event_id == event_id
            self.deliveries.append(event_id)
            return (replace(jobs[0], attempt_count=len(self.deliveries)),)

        async def retry(self, event_id, lease_token, *, next_attempt_at, error_class):
            del lease_token
            assert error_class == "LongTermMemoryConnectionError"
            self.next_attempt_at = next_attempt_at
            self.timeline.append(("retry", event_id))
            self.store.queued.append((event_id, self.store.source_references[event_id]))

    memory = TransientMemory()
    queue = RetryQueue(store)
    native_processor = ProcessMemoryJobUseCase(
        ProcessMemoryUseCase(store, memory), queue, max_attempts=2, retry_delays_seconds=(0.2,)
    )
    outcomes = []

    class RecordingProcessor:
        async def execute(self, job):
            result = await native_processor.execute(job)
            outcomes.append(result.outcome)
            return result

    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    runtime._process_job = RecordingProcessor()
    handle = await runtime.persist_session_a(case)
    ready = await runtime.wait_for_memory(handle, timeout_seconds=2)
    source = artifacts.load_bundle_source("conv01")

    assert ready.status is MemoryReadinessStatus.COMPLETED
    assert outcomes == [MemoryJobProcessOutcome.RETRY, MemoryJobProcessOutcome.COMPLETED]
    assert queue.deliveries == memory.attempted == [handle.event_id, handle.event_id]
    assert queue.not_due_claims >= 1
    assert queue.completed == [handle.event_id]
    assert len(store.appended) == len(source.events) == len(memory.processed) == 1
    assert source.ready and not source.failed

    next_handle = await runtime.persist_session_a(second)
    await runtime.wait_for_memory(next_handle, timeout_seconds=2)
    output = await runtime.run_session_b(
        second, condition=CrossSessionCondition.WITH_LTM, enable_ltm=True, schedule_memory=False
    )
    assert output.retrieved_memories
    assert queue.deliveries == memory.attempted == [handle.event_id, handle.event_id]
    assert len(memory.processed) == sum(store.schedule_flags) == 1
    assert artifacts.load_bundle_source("conv01") == source


@pytest.mark.parametrize("lease_valid", (True, False))
async def test_resume_waits_for_leased_source_event_without_claiming_later_turn(
    tmp_path, lease_valid
):
    case = _cross_case()
    later_pair = tuple(
        message.model_copy(update={"message_id": f"later-{message.message_id}"})
        for message in case.inputs.session_a_messages
    )
    case = case.model_copy(
        update={
            "inputs": case.inputs.model_copy(
                update={"session_a_messages": (*case.inputs.session_a_messages, *later_pair)}
            )
        }
    )
    artifacts, plan = _isolated_store(tmp_path, case)
    store = _BoundaryStore()
    memory = _SharedScopedMemory()

    class LeasedQueue(_ReceiptQueue):
        def __init__(self, store):
            super().__init__(store)
            self.expected_claims = []
            self.lease_valid = lease_valid

        async def claim_due(self, **kwargs):
            raise AssertionError("resume must claim only the expected source event")

        async def claim_expected(self, event_id, **kwargs):
            self.expected_claims.append(event_id)
            if event_id == UUID(int=300) and self.lease_valid:
                self.lease_valid = False
                return ()
            index = next(
                index
                for index, (queued_id, _) in enumerate(self.store.queued)
                if queued_id == event_id
            )
            self.store.queued.insert(0, self.store.queued.pop(index))
            return await _ReceiptQueue.claim_due(self, **kwargs)

    queue = LeasedQueue(store)
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    handle = await runtime.persist_session_a(case)
    first, later = handle.source_event_ids
    assert (first, later) == (UUID(int=300), UUID(int=301))
    # The later pending turn was due before A's abandoned lease, even when A has expired.
    now = datetime.now(UTC)
    due_at = {
        first: now + timedelta(seconds=30 if lease_valid else -1),
        later: now - timedelta(minutes=1),
    }
    store.queued.sort(key=lambda item: due_at[item[0]])
    resumed = ArtifactStore.resume(artifacts.root, expected_identity=artifacts.manifest.identity)
    runtime = _shared_runtime(resumed, plan, store, memory, queue)
    handle = await runtime.persist_session_a(case)
    ready = await runtime.wait_for_memory(handle, timeout_seconds=2)

    assert ready.status is MemoryReadinessStatus.COMPLETED
    assert queue.expected_claims == ([first] if lease_valid else []) + [first, later]
    assert queue.timeline == [
        ("claim", first),
        ("complete", first),
        ("claim", later),
        ("complete", later),
    ]
    assert [source.formation_event_id for source in memory.processed] == [first, later]
    assert len(store.appended) == 2
    assert resumed.load_bundle_source("conv01").ready


async def test_ready_source_missing_receipt_fails_closed_without_replay(tmp_path):
    case = _cross_case()
    second = case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
    artifacts, plan = _isolated_store(tmp_path, case, additional_case_ids=(second.case_id,))
    store = _BoundaryStore()
    memory = _SharedScopedMemory()
    queue = _ReceiptQueue(store)
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    handle = await runtime.persist_session_a(case)
    await runtime.wait_for_memory(handle, timeout_seconds=2)
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)

    class Missing:
        async def inspect(self, *, event_id, user_id):
            return FormationPersistenceSnapshot(
                event_id=event_id, user_id=user_id, memories=(), receipt=None
            )

    runtime._inspector = Missing()
    handle = await runtime.persist_session_a(second)
    with pytest.raises(CrossSessionProtocolError):
        await runtime.wait_for_memory(handle, timeout_seconds=2)
    assert len(memory.processed) == 1
    assert artifacts.load_bundle_source("conv01").failed


async def test_changed_source_is_rejected_instead_of_reusing_another_transcript(tmp_path):
    case = _cross_case()
    second = case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
    artifacts, plan = _isolated_store(tmp_path, case, additional_case_ids=(second.case_id,))
    store = _BoundaryStore()
    memory = _SharedScopedMemory()
    runtime = _shared_runtime(artifacts, plan, store, memory, _ReceiptQueue(store))
    handle = await runtime.persist_session_a(case)
    await runtime.wait_for_memory(handle, timeout_seconds=2)
    changed = second.model_copy(
        update={
            "inputs": second.inputs.model_copy(
                update={
                    "session_a_messages": (
                        second.inputs.session_a_messages[0].model_copy(
                            update={"content": "changed"}
                        ),
                        second.inputs.session_a_messages[1],
                    ),
                }
            )
        }
    )
    with pytest.raises(CrossSessionProtocolError):
        await runtime.persist_session_a(changed)
    assert len(memory.processed) == 1


async def test_resume_committed_receipt_before_checkpoint_never_reextracts(tmp_path, monkeypatch):
    case = _cross_case()
    second = case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
    artifacts, plan = _isolated_store(tmp_path, case, additional_case_ids=(second.case_id,))
    store = _BoundaryStore()
    memory = _SharedScopedMemory()
    queue = _ReceiptQueue(store)
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    handle = await runtime.persist_session_a(case)
    writer = artifacts.write_bundle_source

    class ProcessCrash(BaseException):
        pass

    def crash_on_checkpoint(source):
        if any(event.completed for event in source.events):
            raise ProcessCrash
        return writer(source)

    async def process_is_gone(*_args):
        pass

    monkeypatch.setattr(artifacts, "write_bundle_source", crash_on_checkpoint)
    monkeypatch.setattr(runtime, "_fail_source", process_is_gone)
    with pytest.raises(ProcessCrash):
        await runtime.wait_for_memory(handle, timeout_seconds=2)
    assert len(memory.processed) == 1
    assert queue.completed == list(handle.source_event_ids)
    monkeypatch.setattr(artifacts, "write_bundle_source", writer)
    assert not artifacts.load_bundle_source("conv01").events[0].completed
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    handle = await runtime.persist_session_a(second)
    ready = await runtime.wait_for_memory(handle, timeout_seconds=2)
    assert ready.status is MemoryReadinessStatus.COMPLETED
    assert len(memory.processed) == 1
    assert artifacts.load_bundle_source("conv01").ready


async def test_resume_append_before_checkpoint_reuses_native_source_event(tmp_path, monkeypatch):
    case = _cross_case()
    second = case.model_copy(update={"case_id": "conv01:cross-session:Q02"})
    artifacts, plan = _isolated_store(tmp_path, case, additional_case_ids=(second.case_id,))

    class IdempotentStore(_BoundaryStore):
        async def append_turn(self, user_id, user, assistant, **kwargs):
            for event_id, reference in self.source_references.items():
                if reference.user_id == user_id and reference.turn_id == user.turn_id:
                    return AppendTurnResult(
                        inserted=False, reference=reference, memory_job_event_id=event_id
                    )
            return await super().append_turn(user_id, user, assistant, **kwargs)

    store = IdempotentStore()
    memory = _SharedScopedMemory()
    queue = _ReceiptQueue(store)
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    append = store.append_turn

    async def append_then_interrupt(*args, **kwargs):
        await append(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "append_turn", append_then_interrupt)
    with pytest.raises(asyncio.CancelledError):
        await runtime.persist_session_a(case)
    assert len(store.queued) == 1
    assert not artifacts.load_bundle_source("conv01").events
    monkeypatch.setattr(store, "append_turn", append)
    runtime = _shared_runtime(artifacts, plan, store, memory, queue)
    handle = await runtime.persist_session_a(second)
    ready = await runtime.wait_for_memory(handle, timeout_seconds=2)
    assert ready.status is MemoryReadinessStatus.COMPLETED
    assert len(memory.processed) == 1
    assert len(store.appended) == 1


@pytest.mark.parametrize("global_finishes_first", [True, False])
async def test_scoped_recording_keeps_global_result_in_either_completion_order(
    global_finishes_first,
):
    class Delayed(_Memory):
        async def search_scoped(self, *args, scope, **kwargs):
            if (scope == "global") != global_finishes_first:
                await asyncio.sleep(0.001)
            return (LongTermMemory("global", "Evidence", 0.9),) if scope == "global" else ()

    memory = _RecordingMemory(Delayed())
    await asyncio.gather(
        *[
            memory.search_scoped(
                "user", "query", conversation_id=UUID(int=1), scope=scope, top_k=5, threshold=0.1
            )
            for scope in ("global", "conversation")
        ]
    )
    assert [item.memory_id for item in memory.returned] == ["global"]


async def test_owned_state_init_never_resets_an_existing_source(tmp_path, monkeypatch):
    from evaluation.native_runtime import _initialize_owned_state

    case = _cross_case()
    artifacts, plan = _isolated_store(tmp_path, case)
    initialized = []
    monkeypatch.setattr("evaluation.native_runtime._owned_schema_exists", lambda *_: True)

    async def unexpected(*args):
        initialized.append(args)
        raise AssertionError("resume must not recreate its owned schema")

    monkeypatch.setattr("evaluation.native_runtime._write_owner_marker", unexpected)
    await _initialize_owned_state(_native_config(), plan, artifacts.load_isolation_ledger())
    assert initialized == []
    owned = allocate_case_resources(plan, case_id=case.case_id, attempt=1)
    ledger = register_case_resources(artifacts.load_isolation_ledger(), plan, owned)
    monkeypatch.setattr("evaluation.native_runtime._owned_schema_exists", lambda *_: False)
    with pytest.raises(ValueError, match="state is missing"):
        await _initialize_owned_state(_native_config(), plan, ledger)
