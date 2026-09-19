"""Provider-free tests for native runtime composition and crash ownership."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobResult,
)
from app.config.settings import Settings
from app.domain.models.conversation import AppendTurnResult, CompletedTurnReference
from app.domain.models.kira import KiraEventKind, KiraStreamEvent
from app.domain.models.memory import LongTermMemory
from app.domain.models.memory_job import MemoryJob
from evaluation.artifacts import ArtifactRunIdentity, ArtifactStore
from evaluation.config import EvalConfig, ProviderConfig, load_config
from evaluation.cross_session import (
    CrossSessionCondition,
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
    _persist_message_pairs,
    _RecordingKira,
    _RecordingMemory,
    _RecordingRewriter,
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


def _isolated_store(tmp_path: Path, case: EvalCase) -> tuple[ArtifactStore, IsolationPlan]:
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
        selected_case_ids=(case.case_id,),
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

    async def create_conversation(self, user_id, *, title=None):
        del user_id, title
        session_id = f"managed-session-{len(self.created_sessions) + 1}"
        self.created_sessions.append(session_id)
        return SimpleNamespace(session_id=session_id)

    async def is_conversation_active(self, user_id, session_id):
        del user_id, session_id
        return True

    async def read_recent(self, user_id, session_id, limit):
        del user_id, session_id, limit
        return ()

    async def append_turn(self, user_id, user_message, assistant_message, *, schedule_memory):
        self.schedule_flags.append(schedule_memory)
        result = AppendTurnResult(
            inserted=True,
            reference=CompletedTurnReference(
                user_id=user_id,
                session_id=user_message.session_id,
                conversation_id=UUID(int=len(self.schedule_flags)),
                turn_id=user_message.turn_id,
                boundary_message_id=len(self.schedule_flags) * 2,
            ),
            memory_job_event_id=UUID(int=99) if schedule_memory else None,
        )
        self.last_reference = result.reference
        return result


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
        del kwargs
        assert self.store.last_reference is not None
        return (
            MemoryJob(
                event_id=UUID(int=300),
                reference=self.store.last_reference,
                attempt_count=1,
                lease_token=UUID(int=301),
                lease_expires_at=datetime(2026, 9, 19, 1, tzinfo=UTC),
            ),
        )


class _JobProcessor:
    async def execute(self, job):
        assert job.event_id == UUID(int=300)
        return ProcessMemoryJobResult(MemoryJobProcessOutcome.COMPLETED, 1)


class _Inspector:
    async def inspect(self, *, event_id, user_id):
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
                    turn_id="turn-a",
                    boundary_message_id=2,
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
        inspector=_Inspector(),  # type: ignore[arg-type]
        rewriter=_Rewriter(),  # type: ignore[arg-type]
        kira=backend_kira,  # type: ignore[arg-type]
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
    assert artifacts.load_isolation_ledger().resources[0].memory_ids == (UUID(int=400),)

    with pytest.raises(CrossSessionProtocolError):
        await runtime.run_session_b(
            case,
            condition=CrossSessionCondition.WITH_LTM,
            enable_ltm=True,
            schedule_memory=True,
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
    assert reference.turn_id == "turn-2"


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
