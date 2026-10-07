"""In-process native runtime used by company-PC and internal benchmark profiles.

This composition reuses the production application ports and adapters.  It intentionally does not
add a benchmark endpoint to the Gateway.  The benchmark process owns a run-scoped memory schema,
run-scoped identities and a stopped-worker queue consumer so No-LTM/With-LTM arms cannot mutate
global runtime flags or race another worker.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import sql
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.services.context_builder import ContextBuilder
from app.application.use_cases.handle_chat import HandleChatUseCase
from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobUseCase,
)
from app.config.settings import Settings
from app.domain.models.chat import ChatCommand
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraStreamEvent
from app.domain.models.memory import LongTermMemory
from app.domain.models.memory_job import MemoryJob
from app.domain.ports.kira_client import KiraClientPort
from app.domain.ports.long_term_memory import LongTermMemoryPort
from app.domain.ports.query_rewriter import QueryRewriterPort
from app.infrastructure.kira.http_kira_client import KiraHttpAdapter
from app.infrastructure.llm.vllm_query_rewriter import VllmQueryRewriterAdapter
from app.infrastructure.memory.mem0_adapter import Mem0Adapter, create_mem0_client
from app.infrastructure.memory.postgres_admin import (
    initialize_memory_schema,
    normalize_psycopg_dsn,
)
from app.infrastructure.postgres.client import create_postgres_engine
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import conversations, memory_jobs
from evaluation.artifacts import (
    ArtifactStore,
    BundleSourceArtifact,
    BundleSourceEventArtifact,
    CaseAttemptArtifact,
    DiagnosticArtifact,
    FormedCorpusArtifact,
)
from evaluation.compiler import cross_session_source_sha256
from evaluation.config import EvalConfig
from evaluation.cross_session import (
    CrossSessionCondition,
    CrossSessionDependencyError,
    CrossSessionProtocolError,
    CrossSessionRuntimePort,
    MemoryJobHandle,
    MemoryReadiness,
    MemoryReadinessStatus,
    RetrievedMemory,
    SessionBExecution,
)
from evaluation.formation import (
    FormationCaptureObserver,
    PersistentFormationEvaluator,
    PersistentFormationResult,
    PostgresFormationInspector,
    build_formed_corpus,
    receipt_provenance_matches,
)
from evaluation.isolation import (
    IsolationLedger,
    IsolationPlan,
    allocate_bundle_qa_resources,
    allocate_bundle_resources,
    allocate_case_resources,
    claim_case_resources,
    isolation_plan_sha256,
    kira_benchmark_username,
    register_case_resources,
    register_memory_ids,
)
from evaluation.judge import InternalSemanticJudge, JudgeError
from evaluation.measurement import (
    MeasurementRecorder,
    ProviderStage,
    current_measurement,
    http_measurement_hooks,
    measure_stage,
    measurement_arm,
    merge_measurements,
    record_provider_call,
)
from evaluation.models import (
    CrossSessionInput,
    EvalCase,
    FormationInput,
    Outcome,
    RetrievalInput,
    Suite,
)
from evaluation.native_executor import NativeFormationCaseEvaluation
from evaluation.retrieval import (
    FormationRetrievalFixture,
    FormationRetrievalFixtureManager,
    GoldFixtureClient,
    GoldRetrievalFixture,
    GoldRetrievalFixtureManager,
    RetrievalCaseEvaluation,
    RetrievalEvaluator,
)
from evaluation.scoring import normalize_exact, score_formation
from evaluation.timing import TimingOutcome, TimingStage

_FALLBACK_TIME = datetime(2000, 1, 1, tzinfo=UTC)


def reconcile_interrupted_attempts(
    artifacts: ArtifactStore,
    cases: Sequence[EvalCase],
) -> None:
    """Close a durably allocated attempt that crashed before its case artifact was appended."""

    ledger = artifacts.load_isolation_ledger()
    case_by_id = {case.case_id: case for case in cases}
    for resource in ledger.resources:
        if resource.resource_role == "bundle_source":
            continue
        next_attempt = artifacts.next_attempt_number(resource.case_id)
        if resource.attempt < next_attempt:
            continue
        case = case_by_id.get(resource.case_id)
        if case is None or next_attempt != resource.attempt:
            raise ValueError("isolation ledger contains an invalid interrupted attempt")
        artifact = CaseAttemptArtifact(
            case_id=case.case_id,
            suite=case.suite,
            attempt=resource.attempt,
            completed_at=datetime.now(UTC),
            outcome=Outcome.PROTOCOL_ERROR,
            reason_codes=("benchmark_attempt_interrupted",),
        )
        artifacts.append_case_attempt(artifact)
        artifacts.write_diagnostic(
            DiagnosticArtifact(
                case_id=case.case_id,
                attempt=resource.attempt,
                outcome=Outcome.PROTOCOL_ERROR,
                reason_codes=artifact.reason_codes,
            )
        )


def settings_for_native_runtime(base: Settings, config: EvalConfig) -> Settings:
    """Bind app adapters to the exact evaluated providers and isolated data names."""

    if config.database_url is None or config.memory_database_url is None:
        raise ValueError("native runtime requires conversation and memory databases")
    providers = (config.extraction, config.rewrite, config.embedding, config.judge)
    if not all(provider.configured for provider in providers):
        raise ValueError(
            "native runtime requires explicit extraction/rewrite/embedding/judge providers"
        )
    assert config.extraction.base_url is not None and config.extraction.model is not None
    assert config.rewrite.base_url is not None and config.rewrite.model is not None
    assert config.embedding.base_url is not None and config.embedding.model is not None
    if config.embedding_dimensions is None:
        raise ValueError("native runtime requires a pinned embedding dimension")
    return base.model_copy(
        update={
            "database_url": config.database_url,
            "vllm_base_url": config.rewrite.base_url,
            "vllm_model": config.rewrite.model,
            "vllm_api_key": config.rewrite.api_key,
            "vllm_connect_timeout_seconds": config.connect_timeout_seconds,
            "vllm_read_timeout_seconds": config.read_timeout_seconds,
            "ltm_enabled": True,
            "memory_formation_enabled": False,
            "memory_database_url": config.memory_database_url,
            "memory_admin_database_url": config.memory_database_url,
            "memory_schema": config.memory_schema,
            "memory_collection_name": config.memory_collection,
            "memory_embedding_base_url": config.embedding.base_url,
            "memory_embedding_model": config.embedding.model,
            "memory_embedding_api_key": config.embedding.api_key,
            "memory_embedding_dims": config.embedding_dimensions,
            "memory_embedding_connect_timeout_seconds": config.connect_timeout_seconds,
            "memory_embedding_read_timeout_seconds": config.read_timeout_seconds,
            "memory_llm_base_url": config.extraction.base_url,
            "memory_llm_model": config.extraction.model,
            "memory_llm_api_key": config.extraction.api_key,
            "memory_llm_temperature": config.temperature,
            "memory_llm_max_tokens": config.extraction_max_tokens,
            "memory_operation_timeout_seconds": config.total_timeout_seconds,
            "memory_search_top_k": config.retrieval_top_k,
            "memory_search_threshold": config.retrieval_threshold,
            "memory_search_timeout_seconds": config.read_timeout_seconds,
            "otel_enabled": False,
            **(
                {
                    "kira_base_url": config.kira_base_url,
                    "kira_username": config.kira_username,
                    "kira_domain": config.kira_domain,
                    "kira_basic_auth": config.kira_basic_auth,
                    "kira_service_id": config.kira_service_id,
                    "kira_device": config.kira_device,
                    "kira_message_type": config.kira_message_type,
                }
                if config.kira_configured
                else {}
            ),
        }
    )


class _NoOpStage:
    def set_attribute(self, key: str, value: object) -> None:
        del key, value

    def set_outcome(self, outcome: str) -> None:
        del outcome

    def set_input(self, value: object) -> None:
        del value

    def set_output(self, value: object) -> None:
        del value

    def set_usage(self, usage: Mapping[str, object]) -> None:
        del usage


class _NoOpContextObserver:
    @contextmanager
    def stage(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[_NoOpStage]:
        del name, kind, attributes
        yield _NoOpStage()

    def request_attribute(self, key: str, value: object) -> None:
        del key, value

    def capture_telemetry_context(self, correlation_id: str) -> None:
        del correlation_id
        return None

    def context_observed(self, message_count: int, estimated_tokens: int) -> None:
        del message_count, estimated_tokens

    def memory_search_observed(self, outcome: str, result_count, seconds) -> None:
        del outcome, result_count, seconds

    def memory_job_schedule_observed(self, outcome: str) -> None:
        del outcome

    def rewrite_observed(self, outcome: str, seconds) -> None:
        del outcome, seconds

    def degraded(
        self,
        correlation_id: str,
        operation: str,
        error_class: str,
        fallback: str,
    ) -> None:
        del correlation_id, operation, error_class, fallback

    def conversation_write_observed(self, outcome: str) -> None:
        del outcome


class _RecordingMemory(LongTermMemoryPort):
    def __init__(self, memory: LongTermMemoryPort | None) -> None:
        self._memory = memory
        self.returned: tuple[LongTermMemory, ...] = ()
        self._scoped_returned: dict[str, tuple[LongTermMemory, ...]] = {}

    async def search(
        self, user_id: str, query: str, *, top_k: int, threshold: float
    ) -> tuple[LongTermMemory, ...]:
        if self._memory is None:
            self.returned = ()
        else:
            with measure_stage(TimingStage.RETRIEVAL):
                self.returned = await self._memory.search(
                    user_id,
                    query,
                    top_k=top_k,
                    threshold=threshold,
                )
        return self.returned

    async def search_scoped(
        self,
        user_id: str,
        query: str,
        *,
        conversation_id: UUID,
        scope: str,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        if self._memory is None:
            returned = ()
        else:
            with measure_stage(TimingStage.RETRIEVAL):
                returned = await self._memory.search_scoped(
                    user_id,
                    query,
                    conversation_id=conversation_id,
                    scope=scope,
                    top_k=top_k,
                    threshold=threshold,
                )
        self._scoped_returned[scope] = returned
        self.returned = tuple(
            {
                item.memory_id: item for values in self._scoped_returned.values() for item in values
            }.values()
        )
        return returned

    async def process_memory(self, source: object):
        if self._memory is None:  # pragma: no cover - never used by Session B
            raise RuntimeError("memory disabled")
        return await self._memory.process_memory(source)  # type: ignore[arg-type]


class _RecordingRewriter(QueryRewriterPort):
    def __init__(self, rewriter: QueryRewriterPort) -> None:
        self._rewriter = rewriter
        self.output: str | None = None

    async def rewrite(self, context) -> str:
        with measure_stage(TimingStage.REWRITE):
            self.output = await self._rewriter.rewrite(context)
        return self.output


class _RecordingKira(KiraClientPort):
    def __init__(self, client: KiraClientPort) -> None:
        self._client = client
        self.messages: list[str] = []
        self.events: list[KiraStreamEvent] = []

    async def chat_stream(self, message: str) -> AsyncIterator[KiraStreamEvent]:
        self.messages.append(message)
        recorder = current_measurement()
        timer = recorder.timer().start_kira() if recorder is not None else None
        try:
            source = await self._client.chat_stream(message)
        except BaseException:
            if timer is not None:
                timer.finish(TimingOutcome.ERROR)
            record_provider_call(ProviderStage.KIRA, basis="kira_chat", outcome="error")
            raise

        async def recorded() -> AsyncIterator[KiraStreamEvent]:
            first_token = False
            outcome = TimingOutcome.ERROR
            try:
                async for event in source:
                    self.events.append(event)
                    if event.text_fragment and not first_token:
                        first_token = True
                        if timer is not None:
                            timer.first_token()
                    yield event
                outcome = TimingOutcome.SUCCESS
            finally:
                if timer is not None:
                    timer.finish(outcome)
                record_provider_call(
                    ProviderStage.KIRA,
                    basis="kira_chat",
                    outcome="success" if outcome is TimingOutcome.SUCCESS else "error",
                )

        return recorded()

    async def invalidate_token(self) -> None:
        await self._client.invalidate_token()


class PersistentFormationRuntime:
    """Persist exact source turns, then run native formation and durable reconciliation."""

    def __init__(
        self,
        *,
        plan: IsolationPlan,
        ledger: IsolationLedger,
        artifacts: ArtifactStore,
        conversation_store: PostgresConversationStoreAdapter,
        evaluator: PersistentFormationEvaluator,
    ) -> None:
        self.plan = plan
        self.ledger = ledger
        self.artifacts = artifacts
        self._store = conversation_store
        self._evaluator = evaluator

    async def evaluate(self, case: EvalCase) -> PersistentFormationResult:
        if not isinstance(case.inputs, FormationInput):
            raise ValueError("persistent formation runtime accepts formation cases only")
        attempt = self.artifacts.next_attempt_number(case.case_id)
        self.ledger = self.artifacts.load_isolation_ledger()
        allocated = allocate_case_resources(self.plan, case_id=case.case_id, attempt=attempt)
        self.ledger = register_case_resources(self.ledger, self.plan, allocated)
        self.artifacts.write_isolation_ledger(self.ledger)
        reference = await _persist_message_pairs(
            self._store,
            user_id=allocated.user_id,
            session_id=allocated.session_id,
            messages=case.inputs.messages,
            schedule_last=False,
        )
        resource = claim_case_resources(
            self.plan,
            case_id=case.case_id,
            attempt=attempt,
            conversation_id=reference.conversation_id,
            event_id=allocated.event_id,
        )
        self.ledger = register_case_resources(self.ledger, self.plan, resource)
        self.artifacts.write_isolation_ledger(self.ledger)
        with measure_stage(TimingStage.FORMATION):
            result = await self._evaluator.evaluate(
                case_id=case.case_id,
                family_id=case.family_id,
                source_gold_ids=tuple(fact.gold_id for fact in case.gold.facts),
                inputs=case.inputs,
                reference=reference,
                event_id=resource.event_id,
            )
        if result.persistence is not None:
            memory_ids = tuple(memory.memory_id for memory in result.persistence.memories)
            self.ledger = register_memory_ids(
                self.ledger,
                self.plan,
                case_id=case.case_id,
                attempt=attempt,
                memory_ids=memory_ids,
            )
            self.artifacts.write_isolation_ledger(self.ledger)
        return result

    def build_or_load_corpus(self) -> FormedCorpusArtifact:
        path = self.artifacts.root / "formed-corpus.json"
        if path.exists():
            return self.artifacts.load_formed_corpus()
        results: list[PersistentFormationResult] = []
        formation_ids = {
            case_id
            for case_id in self.artifacts.manifest.identity.selected_case_ids
            if ":formation:" in case_id
        }
        attempts = {attempt.case_id: attempt for attempt in self.artifacts.latest_attempts}
        if not formation_ids.issubset(attempts):
            raise ValueError("formation suite is incomplete")
        for case_id in sorted(formation_ids):
            output = attempts[case_id].output
            if not isinstance(output, Mapping):
                raise ValueError("formation suite has unresolved execution errors")
            evaluated = NativeFormationCaseEvaluation.model_validate(output)
            if evaluated.persistence is None:
                raise ValueError("persistent formation evidence is missing")
            results.append(evaluated.persistence)
        corpus = build_formed_corpus(
            identity=self.artifacts.manifest.identity,
            results=results,
            created_at=datetime.now(UTC),
        )
        self.artifacts.write_formed_corpus(corpus)
        return corpus


class PersistentRetrievalRuntime:
    """Lazily materialize both owned retrieval corpora after formation completes."""

    def __init__(
        self,
        *,
        plan: IsolationPlan,
        cases: Sequence[EvalCase],
        client: GoldFixtureClient,
        evaluator: RetrievalEvaluator,
        formation: PersistentFormationRuntime,
        conversation_store: PostgresConversationStoreAdapter | None = None,
    ) -> None:
        self._cases = tuple(case for case in cases if isinstance(case.inputs, RetrievalInput))
        self._evaluator = evaluator
        self._formation = formation
        self._gold_manager = GoldRetrievalFixtureManager(client, plan, conversation_store)
        self._formed_manager = FormationRetrievalFixtureManager(client, plan, conversation_store)
        self._gold: GoldRetrievalFixture | None = None
        self._formed: FormationRetrievalFixture | None = None
        self._lock = asyncio.Lock()

    async def _setup(self) -> None:
        if self._gold is not None and self._formed is not None:
            return
        async with self._lock:
            if self._gold is not None and self._formed is not None:
                return
            corpus = self._formation.build_or_load_corpus()
            gold: GoldRetrievalFixture | None = None
            try:
                gold = await self._gold_manager.setup(self._cases)
                formed = await self._formed_manager.setup(corpus, self._cases)
            except BaseException:
                if gold is not None:
                    await self._gold_manager.cleanup(gold)
                raise
            self._gold = gold
            self._formed = formed

    async def evaluate_gold(self, case: EvalCase) -> RetrievalCaseEvaluation:
        await self._setup()
        return await self._evaluator.evaluate_gold_fixture(case, self._gold)

    async def evaluate_formed(self, case: EvalCase) -> RetrievalCaseEvaluation:
        await self._setup()
        return await self._evaluator.evaluate_formation_produced(case, self._formed)

    async def cleanup(self) -> None:
        errors: list[Exception] = []
        if self._formed is not None:
            try:
                await self._formed_manager.cleanup(self._formed)
            except Exception as error:
                errors.append(error)
            self._formed = None
        if self._gold is not None:
            try:
                await self._gold_manager.cleanup(self._gold)
            except Exception as error:
                errors.append(error)
            self._gold = None
        if errors:
            raise RuntimeError("retrieval_fixture_cleanup_failed")


@dataclass(frozen=True, slots=True)
class _PendingTrajectory:
    case: EvalCase
    attempt: int
    user_id: str
    references: Mapping[UUID, CompletedTurnReference]
    bundle_id: str


class _SourceMemoryJobQueue(PostgresMemoryJobQueueAdapter):
    """Keep sequential source formation ordered while retaining native queue leases."""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine)
        self._expected_event: ContextVar[UUID | None] = ContextVar(
            "benchmark_expected_memory_event", default=None
        )

    async def claim_expected(self, event_id: UUID, **kwargs: Any) -> tuple[MemoryJob, ...]:
        token = self._expected_event.set(event_id)
        try:
            return await self.claim_due(**kwargs)
        finally:
            self._expected_event.reset(token)

    def _claim_candidates(self, limit: int, max_attempts: int):
        event_id = self._expected_event.get()
        if event_id is None:
            raise CrossSessionProtocolError
        return (
            super()._claim_candidates(limit, max_attempts).where(memory_jobs.c.event_id == event_id)
        )


class NativeCrossSessionRuntime(CrossSessionRuntimePort):
    """Form one shared source per bundle and own isolated Session-B QA executions."""

    def __init__(
        self,
        *,
        plan: IsolationPlan,
        ledger: IsolationLedger,
        artifacts: ArtifactStore,
        settings: Settings,
        conversation_store: PostgresConversationStoreAdapter,
        memory: Mem0Adapter,
        memory_queue: PostgresMemoryJobQueueAdapter,
        process_job: ProcessMemoryJobUseCase,
        inspector: PostgresFormationInspector,
        rewriter: VllmQueryRewriterAdapter,
        kira_factory: Callable[[str], KiraClientPort],
        judge: InternalSemanticJudge,
        gold_fact_text: Mapping[str, str],
        gold_fact_source_ids: Mapping[str, Sequence[str]] | None = None,
        failed_source_cleanup: Callable[[str, Sequence[str]], Awaitable[None]] | None = None,
        recover_receipted_job: Callable[[UUID, CompletedTurnReference, int], Awaitable[None]]
        | None = None,
    ) -> None:
        self.plan = plan

        self.ledger = ledger
        self.artifacts = artifacts
        self._settings = settings
        self._store = conversation_store
        self._memory = memory
        self._queue = memory_queue
        self._process_job = process_job
        self._inspector = inspector
        self._rewriter = rewriter
        self._kira_factory = kira_factory
        self._judge = judge
        self._gold_fact_text = dict(gold_fact_text)
        self._gold_fact_source_ids = (
            dict(gold_fact_source_ids) if gold_fact_source_ids is not None else None
        )
        self._pending: dict[UUID, _PendingTrajectory] = {}
        self._abort_cleanup_failed = False
        self._failed_source_cleanup = failed_source_cleanup
        self._recover_receipted_job = recover_receipted_job
        self._validated_sources: set[str] = set()
        self._resuming_sources: set[str] = set()
        self._source_recorders: dict[str, MeasurementRecorder] = {}
        self._source_measurement_baselines = {}

    async def persist_session_a(self, case: EvalCase) -> MemoryJobHandle:
        if self._abort_cleanup_failed:
            raise CrossSessionDependencyError
        if not isinstance(case.inputs, CrossSessionInput):
            raise ValueError("cross-session runtime accepts cross-session cases only")
        bundle_id = case.case_id.split(":")[0]
        attempt = self.artifacts.next_attempt_number(case.case_id)
        self.ledger = self.artifacts.load_isolation_ledger()
        allocated = allocate_bundle_resources(self.plan, bundle_id=bundle_id)
        source_owner = next(
            (
                item
                for item in self.ledger.resources
                if item.case_id == allocated.case_id and item.attempt == 1
            ),
            None,
        )
        if source_owner is None:
            self.ledger = register_case_resources(self.ledger, self.plan, allocated)
            source_owner = allocated
        qa = allocate_bundle_qa_resources(
            self.plan, bundle_id=bundle_id, case_id=case.case_id, attempt=attempt
        )
        self.ledger = register_case_resources(self.ledger, self.plan, qa)
        self.artifacts.write_isolation_ledger(self.ledger)
        pairs = self._ordered_pairs(case)
        source_hash = cross_session_source_sha256(case.inputs)
        try:
            source = self.artifacts.load_bundle_source(bundle_id)
            self._resuming_sources.add(bundle_id)
        except FileNotFoundError:
            source = BundleSourceArtifact(
                identity=self.artifacts.manifest.identity,
                bundle_id=bundle_id,
                logical_user_id=case.inputs.user_id,
                source_sha256=source_hash,
                persisted_user_id=allocated.user_id,
                source_session_id=allocated.session_id,
                expected_source_events=len(pairs),
            )
            self.artifacts.write_bundle_source(source)
        if (
            source.source_sha256 != source_hash
            or source.persisted_user_id != allocated.user_id
            or source.logical_user_id != case.inputs.user_id
        ):
            raise CrossSessionProtocolError
        if source.failed:
            raise CrossSessionDependencyError
        recorder = self._source_recorders.setdefault(bundle_id, MeasurementRecorder())
        self._source_measurement_baselines.setdefault(bundle_id, source.measurement)
        try:
            # A completed append can be replayed after a crash; native append_turn returns
            # its original boundary/event. This does not redeliver extraction.
            with recorder.bind():
                for index in range(len(source.events), len(pairs)):
                    user, assistant = pairs[index]
                    base_time = user.timestamp or (
                        _FALLBACK_TIME + timedelta(microseconds=index * 2)
                    )
                    turn_id = _owned_turn_id(
                        allocated.user_id, allocated.session_id, user.message_id
                    )
                    appended = await self._store.append_turn(
                        allocated.user_id,
                        ConversationMessage(
                            allocated.session_id,
                            turn_id,
                            ConversationRole.USER,
                            user.content,
                            base_time,
                        ),
                        ConversationMessage(
                            allocated.session_id,
                            turn_id,
                            ConversationRole.ASSISTANT,
                            assistant.content,
                            assistant.timestamp or (base_time + timedelta(microseconds=1)),
                        ),
                        schedule_memory=True,
                    )
                    if appended.memory_job_event_id is None:
                        raise CrossSessionProtocolError
                    reference = appended.reference
                    if source.source_conversation_id is not None and (
                        reference.conversation_id != source.source_conversation_id
                    ):
                        raise CrossSessionProtocolError
                    event = BundleSourceEventArtifact(
                        event_id=appended.memory_job_event_id,
                        user_id=reference.user_id,
                        session_id=reference.session_id,
                        conversation_id=reference.conversation_id,
                        turn_id=reference.turn_id,
                        boundary_message_id=reference.boundary_message_id,
                        source_timestamp=base_time,
                    )
                    source = source.model_copy(
                        update={
                            "source_conversation_id": reference.conversation_id,
                            "events": (*source.events, event),
                        }
                    )
                    self.artifacts.write_bundle_source(source)
        except asyncio.CancelledError:
            raise
        except BaseException:
            await self._fail_source(source, "source_append_failed")
            raise
        final = source.events[-1]
        resource = source_owner.model_copy(
            update={
                "conversation_id": final.conversation_id,
                "event_id": final.event_id,
            }
        )
        self.ledger = register_case_resources(self.ledger, self.plan, resource)
        self.artifacts.write_isolation_ledger(self.ledger)
        references = {event.event_id: self._reference(event) for event in source.events}
        self._pending[final.event_id] = _PendingTrajectory(
            case, attempt, allocated.user_id, references, bundle_id
        )
        return MemoryJobHandle(event_id=final.event_id, source_event_ids=tuple(references))

    @staticmethod
    def _ordered_pairs(case: EvalCase):
        pairs = _message_pairs(case.inputs.session_a_messages)
        return tuple(
            pair
            for _, pair in sorted(
                enumerate(pairs), key=lambda item: (item[1][0].timestamp or _FALLBACK_TIME, item[0])
            )
        )

    @staticmethod
    def _reference(event: BundleSourceEventArtifact) -> CompletedTurnReference:
        return CompletedTurnReference(
            user_id=event.user_id,
            session_id=event.session_id,
            conversation_id=event.conversation_id,
            turn_id=event.turn_id,
            boundary_message_id=event.boundary_message_id,
        )

    async def _fail_source(self, source: BundleSourceArtifact, code: str) -> None:
        persisted = self.artifacts.load_bundle_source(source.bundle_id)
        if persisted.failed:
            return
        recorder = self._source_recorders.get(source.bundle_id)
        measurement = (
            merge_measurements(
                self._source_measurement_baselines.get(source.bundle_id), recorder.snapshot()
            )
            if recorder is not None
            else source.measurement
        )
        source = source.model_copy(
            update={
                "failed": True,
                "failure_code": code,
                "ready": False,
                "measurement": measurement,
            }
        )
        self.artifacts.write_bundle_source(source)
        await self._clear_failed_sources(source.persisted_user_id, (source.source_session_id,))

    async def _clear_failed_sources(
        self, user_id: str, session_ids: Sequence[str] | set[str]
    ) -> None:
        """Use native deletion/fencing so abandoned jobs cannot contaminate the next case."""

        try:
            resource = next(
                (item for item in self.ledger.resources if item.user_id == user_id), None
            )
            if resource is None or any(
                session_id != resource.session_id
                and not session_id.startswith(f"{resource.session_id}:")
                for session_id in session_ids
            ):
                raise CrossSessionProtocolError
            if not callable(getattr(self._store, "mark_deletion_pending", None)) or not callable(
                getattr(self._store, "purge_deletion_pending", None)
            ):
                # The frozen historical runtime predates conversation-deletion ports. Its
                # shared-harness SQL callback erases only this disposable case's exact owner.
                if self._failed_source_cleanup is None:
                    raise CrossSessionDependencyError
                await self._failed_source_cleanup(user_id, tuple(session_ids))
                return
            # Fence every source before purging any, including a potentially delayed formation
            # call that was cancelled while its provider thread was still finishing.
            for session_id in sorted(set(session_ids)):
                await self._store.mark_deletion_pending(user_id, session_id)
            for session_id in sorted(set(session_ids)):
                await self._store.purge_deletion_pending(user_id, session_id)
        except BaseException:
            # No further case may claim from this queue until run cleanup/resume succeeds.
            self._abort_cleanup_failed = True
            raise CrossSessionDependencyError from None

    async def wait_for_memory(
        self,
        handle: MemoryJobHandle,
        *,
        timeout_seconds: float,
    ) -> MemoryReadiness:
        pending = self._pending.pop(handle.event_id, None)
        if pending is None:
            raise CrossSessionProtocolError
        source = self.artifacts.load_bundle_source(pending.bundle_id)
        if source.failed:
            raise CrossSessionDependencyError
        if source.ready and source.bundle_id in self._validated_sources:
            return self._readiness(source, handle.event_id)
        completed = False
        recorder = self._source_recorders.setdefault(source.bundle_id, MeasurementRecorder())
        try:
            with recorder.bind(), measure_stage(TimingStage.MEMORY_READINESS):
                async with asyncio.timeout(timeout_seconds):
                    for index, event in enumerate(source.events):
                        reference = self._reference(event)
                        snapshot = (
                            await self._inspector.inspect(
                                event_id=event.event_id, user_id=pending.user_id
                            )
                            if event.completed or source.bundle_id in self._resuming_sources
                            else None
                        )
                        if snapshot is None or snapshot.receipt is None:
                            if event.completed or source.ready:
                                raise CrossSessionProtocolError
                            while True:
                                claim_expected = getattr(self._queue, "claim_expected", None)
                                claim_options = dict(
                                    lease_owner=uuid4(),
                                    limit=1,
                                    lease_seconds=max(
                                        self._settings.memory_operation_timeout_seconds + 10, 30
                                    ),
                                    max_attempts=2,
                                )
                                jobs = (
                                    await claim_expected(event.event_id, **claim_options)
                                    if callable(claim_expected)
                                    else await self._queue.claim_due(**claim_options)
                                )
                                if not jobs:
                                    # Native retry due times and abandoned leases can delay
                                    # this event. The readiness timeout bounds waiting.
                                    await asyncio.sleep(0.1)
                                    continue
                                if (
                                    len(jobs) != 1
                                    or jobs[0].event_id != event.event_id
                                    or jobs[0].reference != reference
                                ):
                                    raise CrossSessionProtocolError
                                with measure_stage(TimingStage.FORMATION):
                                    result = await self._process_job.execute(jobs[0])
                                if result.outcome is MemoryJobProcessOutcome.RETRY:
                                    continue
                                if result.outcome is MemoryJobProcessOutcome.DEAD:
                                    await self._fail_source(source, "source_job_dead")
                                    completed = True
                                    return MemoryReadiness(
                                        event_id=handle.event_id, status=MemoryReadinessStatus.DEAD
                                    )
                                if result.outcome is not MemoryJobProcessOutcome.COMPLETED:
                                    raise CrossSessionDependencyError
                                break
                            snapshot = await self._inspector.inspect(
                                event_id=event.event_id, user_id=pending.user_id
                            )
                            if (
                                snapshot.receipt is None
                                or snapshot.receipt.memory_count != result.lifecycle_event_count
                            ):
                                raise CrossSessionProtocolError
                        self._validate_snapshot(snapshot, event, reference)
                        if self._recover_receipted_job is not None:
                            await self._recover_receipted_job(
                                event.event_id, reference, snapshot.receipt.memory_count
                            )
                        memory_ids = tuple(memory.memory_id for memory in snapshot.memories)
                        if event.completed and set(event.memory_ids) != set(memory_ids):
                            raise CrossSessionProtocolError
                        if event.completed:
                            continue
                        logical_turn = self._ordered_pairs(pending.case)[index][0].message_id
                        mapping = await self._map_gold(
                            pending.case, snapshot.receipt.events, source_turn_id=logical_turn
                        )
                        events = list(source.events)
                        events[index] = event.model_copy(
                            update={"completed": True, "memory_ids": memory_ids}
                        )
                        source = source.model_copy(
                            update={
                                "events": tuple(events),
                                "memory_gold_ids": {
                                    **source.memory_gold_ids,
                                    **{UUID(key): value for key, value in mapping.items()},
                                },
                                "measurement": merge_measurements(
                                    self._source_measurement_baselines.get(source.bundle_id),
                                    recorder.snapshot(),
                                ),
                            }
                        )
                        self.ledger = self.artifacts.load_isolation_ledger()
                        owner = allocate_bundle_resources(self.plan, bundle_id=source.bundle_id)
                        self.ledger = register_memory_ids(
                            self.ledger,
                            self.plan,
                            case_id=owner.case_id,
                            attempt=1,
                            memory_ids=tuple(
                                dict.fromkeys(
                                    memory_id
                                    for item in source.events
                                    for memory_id in item.memory_ids
                                )
                            ),
                        )
                        self.artifacts.write_isolation_ledger(self.ledger)
                        self.artifacts.write_bundle_source(source)
            if not source.ready:
                source = source.model_copy(
                    update={
                        "ready": True,
                        "measurement": merge_measurements(
                            self._source_measurement_baselines.get(source.bundle_id),
                            recorder.snapshot(),
                        ),
                    }
                )
                self.artifacts.write_bundle_source(source)
            self._validated_sources.add(source.bundle_id)
            completed = True
            return self._readiness(source, handle.event_id)
        except asyncio.CancelledError:
            # No source deletion on an interrupted run: receipt+boundary are the
            # resume key. An enclosing timeout cancels this task internally; the
            # evaluator records its failure while preserving resumable progress.
            raise
        except BaseException:
            if not completed:
                await self._fail_source(source, "source_formation_failed")
            raise
        finally:
            latest = self.artifacts.load_bundle_source(source.bundle_id)
            if latest.failed or (not latest.ready and not completed):
                latest = latest.model_copy(
                    update={
                        "measurement": merge_measurements(
                            self._source_measurement_baselines.get(source.bundle_id),
                            recorder.snapshot(),
                        )
                    }
                )
                self.artifacts.write_bundle_source(latest)

    def _validate_snapshot(self, snapshot, event, reference) -> None:
        if (
            snapshot.event_id != event.event_id
            or snapshot.user_id != event.user_id
            or snapshot.receipt is None
            or not receipt_provenance_matches(
                snapshot.receipt,
                reference,
                historical_control_runtime=getattr(
                    self._inspector, "historical_control_runtime", False
                )
                is True,
            )
            or any(
                memory.conversation_id != reference.conversation_id
                or memory.turn_id != reference.turn_id
                or memory.boundary_message_id != reference.boundary_message_id
                for memory in snapshot.memories
            )
            or {memory.memory_id for memory in snapshot.memories}
            != {item.memory_id for item in snapshot.receipt.events}
        ):
            raise CrossSessionProtocolError

    @staticmethod
    def _readiness(source, event_id) -> MemoryReadiness:
        return MemoryReadiness(
            event_id=event_id,
            status=MemoryReadinessStatus.COMPLETED,
            gold_ids_by_memory_id={
                str(key): value for key, value in source.memory_gold_ids.items()
            },
        )

    async def _map_gold(
        self, case: EvalCase, events, *, source_turn_id: str | None = None
    ) -> dict[str, tuple[str, ...]]:
        relevant = {
            gold_id: self._gold_fact_text[gold_id]
            for gold_id in self._gold_fact_text
            if gold_id.startswith(f"{case.case_id.split(':')[0]}:")
            and (
                self._gold_fact_source_ids is None
                or source_turn_id in self._gold_fact_source_ids.get(gold_id, ())
            )
        }
        predictions = tuple(event.memory for event in events)
        score = score_formation(relevant, predictions)
        if score.needs_judge_prediction_indexes:
            try:
                decisions = await self._judge.formation(
                    predicted_facts=predictions,
                    gold_facts=relevant,
                    prediction_indexes=score.needs_judge_prediction_indexes,
                )
            except JudgeError as error:
                if error.outcome is Outcome.DEPENDENCY_ERROR:
                    raise CrossSessionDependencyError from None
                raise CrossSessionProtocolError from None
            score = score_formation(relevant, predictions, judge_decisions=decisions)
        by_prediction = {match.prediction_index: match.gold_id for match in score.matches}
        # ID mapping is classification, not one-to-one formation scoring. Independent source
        # assertions with the same text must each remain visible in retrieval diagnostics.
        mapped_text: dict[str, list[str]] = {}
        for index, gold_id in by_prediction.items():
            mapped_text.setdefault(normalize_exact(predictions[index]), []).append(gold_id)
        return {
            str(event.memory_id): (
                tuple(mapped_text[normalize_exact(event.memory)])
                if normalize_exact(event.memory) in mapped_text
                else ()
            )
            for event in events
        }

    async def run_session_b(
        self,
        case: EvalCase,
        *,
        condition: CrossSessionCondition,
        enable_ltm: bool,
        schedule_memory: bool,
    ) -> SessionBExecution:
        with measurement_arm(condition.value):
            return await self._run_session_b(
                case, condition=condition, enable_ltm=enable_ltm, schedule_memory=schedule_memory
            )

    async def _run_session_b(
        self,
        case: EvalCase,
        *,
        condition: CrossSessionCondition,
        enable_ltm: bool,
        schedule_memory: bool,
    ) -> SessionBExecution:
        if schedule_memory:
            raise CrossSessionProtocolError
        if not isinstance(case.inputs, CrossSessionInput):
            raise ValueError("cross-session runtime accepts cross-session cases only")
        resource = next(
            item
            for item in self.ledger.resources
            if item.case_id == case.case_id
            and item.attempt == self.artifacts.next_attempt_number(case.case_id)
        )
        memory = _RecordingMemory(self._memory if enable_ltm else None)
        rewriter = _RecordingRewriter(self._rewriter)
        username = kira_benchmark_username(
            self.plan,
            case_id=case.case_id,
            attempt=resource.attempt,
            arm=condition.value,
        )
        kira = _RecordingKira(self._kira_factory(username))
        use_case = HandleChatUseCase(
            kira,
            conversation_store=self._store,
            query_rewriter=rewriter,
            context_builder=ContextBuilder(
                max_recent_messages=self._settings.max_recent_messages,
                recent_token_budget=self._settings.recent_context_token_budget,
                max_long_term_memories=self._settings.memory_search_top_k,
            ),
            observer=cast(Any, _NoOpContextObserver()),
            max_recent_messages=self._settings.max_recent_messages,
            store_timeout_seconds=self._settings.conversation_operation_timeout_seconds,
            long_term_memory=memory if enable_ltm else None,
            memory_search_top_k=self._settings.memory_search_top_k,
            memory_search_threshold=self._settings.memory_search_threshold,
            memory_search_timeout_seconds=self._settings.memory_search_timeout_seconds,
            memory_formation_enabled=False,
        )
        creator = getattr(self._store, "create_conversation", None)
        if callable(creator):
            # Use the product's empty conversation creation. No bootstrap message
            # enters recent context, and every case/arm/attempt gets fresh history.
            conversation = await creator(resource.user_id)
            actual_session = conversation.session_id
        else:
            # Explicit historical comparison retains its old adapter boundary.
            actual_session = f"{resource.session_id}:{condition.value}"
            seed_turn = _owned_turn_id(resource.user_id, actual_session, "seed")
            await self._store.append_turn(
                resource.user_id,
                ConversationMessage(
                    actual_session,
                    seed_turn,
                    ConversationRole.USER,
                    "benchmark bootstrap",
                    datetime.now(UTC),
                ),
                ConversationMessage(
                    actual_session,
                    seed_turn,
                    ConversationRole.ASSISTANT,
                    "benchmark bootstrap",
                    datetime.now(UTC),
                ),
            )
        session = await use_case.execute(
            ChatCommand(session_id=actual_session, message=case.inputs.session_b_query),
            principal=AuthenticatedPrincipal(user_id=resource.user_id),
            correlation_id=uuid4().hex,
            turn_id=_owned_turn_id(
                resource.user_id, actual_session, f"{case.case_id}:{condition.value}"
            ),
        )
        async for _ in session:
            pass
        if (
            not session.source_exhausted
            or not session.final_text.strip()
            or len(kira.messages) != 1
        ):
            raise CrossSessionProtocolError
        for item in memory.returned:
            owner = item.metadata.get("user_id")
            if owner is not None and owner != resource.user_id:
                raise CrossSessionProtocolError
            if any(
                str(memory_id) == item.memory_id and owned.user_id != resource.user_id
                for owned in self.ledger.resources
                if owned.resource_role == "bundle_source"
                for memory_id in owned.memory_ids
            ):
                raise CrossSessionProtocolError
        rewritten = rewriter.output or case.inputs.session_b_query
        return SessionBExecution(
            condition=condition,
            user_id=case.inputs.user_id,
            session_id=case.inputs.session_b,
            current_query=case.inputs.session_b_query,
            provider_id=f"kira-service-{self._settings.kira_service_id}",
            kira_context_identity_sha256=hashlib.sha256(username.encode()).hexdigest(),
            retrieved_memories=tuple(
                RetrievedMemory(
                    memory_id=item.memory_id,
                    user_id=case.inputs.user_id,
                    score=item.score,
                )
                for item in memory.returned
            ),
            rewritten_query=rewritten,
            final_answer=session.final_text.strip(),
            query_memory_event_id=None,
        )


def _owned_turn_id(user_id: str, session_id: str, logical_turn: str) -> str:
    value = f"{user_id}\0{session_id}\0{logical_turn}".encode()
    return f"eval-turn-{hashlib.sha256(value).hexdigest()}"


async def _persist_message_pairs(
    store: PostgresConversationStoreAdapter,
    *,
    user_id: str,
    session_id: str,
    messages: Sequence,
    schedule_last: bool,
):
    reference = None
    for pair_index, (user, assistant) in enumerate(_message_pairs(messages)):
        index = pair_index * 2
        turn_id = _owned_turn_id(user_id, session_id, user.message_id)
        base_time = user.timestamp or (_FALLBACK_TIME + timedelta(microseconds=index))
        assistant_time = assistant.timestamp or (base_time + timedelta(microseconds=1))
        appended = await store.append_turn(
            user_id,
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.USER,
                user.content,
                base_time,
            ),
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.ASSISTANT,
                assistant.content,
                assistant_time,
            ),
            schedule_memory=schedule_last and index + 2 == len(messages),
        )
        if not appended.inserted:
            raise ValueError("native case state is not fresh")
        reference = appended.reference
    assert reference is not None
    return reference


def _message_pairs(messages: Sequence):
    if not messages or len(messages) % 2:
        raise ValueError("native formation needs complete user/assistant pairs")
    pairs = tuple(zip(messages[::2], messages[1::2], strict=True))
    for user, assistant in pairs:
        if user.role != "user" or assistant.role != "assistant":
            raise ValueError("native formation messages must be ordered pairs")
        if user.session_id != assistant.session_id:
            raise ValueError("native formation pairs must belong to one source conversation")
    return pairs


class NativeRuntimeResources:
    """Owned clients and native evaluators for one sequential benchmark invocation."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine: AsyncEngine,
        http_clients: tuple[httpx.AsyncClient, ...],
        memory: Mem0Adapter,
        formation: PersistentFormationRuntime,
        retrieval: PersistentRetrievalRuntime,
        cross_session: NativeCrossSessionRuntime,
        rewriter: VllmQueryRewriterAdapter,
        judge: InternalSemanticJudge,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.http_clients = http_clients
        self.memory = memory
        self.formation = formation
        self.retrieval = retrieval
        self.cross_session = cross_session
        self.rewriter = rewriter
        self.judge = judge
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.memory.close()
        finally:
            try:
                for client in self.http_clients:
                    await client.aclose()
            finally:
                await self.engine.dispose()


def _has_conversation_lifecycle(store: PostgresConversationStoreAdapter) -> bool:
    """Runtime-legacy adapters predate the conversation lifecycle ports."""

    return all(
        callable(getattr(store, name, None))
        for name in ("mark_deletion_pending", "purge_deletion_pending")
    )


async def create_native_runtime(
    *,
    config: EvalConfig,
    base_settings: Settings,
    plan: IsolationPlan,
    ledger: IsolationLedger,
    artifacts: ArtifactStore,
    cases: Sequence[EvalCase],
) -> NativeRuntimeResources:
    """Validate and compose exact native adapters; no provider fallback is permitted."""

    if Suite.CROSS_SESSION in config.suites and not config.kira_configured:
        raise ValueError("cross-session runtime requires explicit KiRa unique_username isolation")
    settings = settings_for_native_runtime(base_settings, config)
    await _initialize_owned_state(config, plan, ledger)
    await initialize_memory_schema(settings)
    engine = create_postgres_engine(settings)
    store = PostgresConversationStoreAdapter(
        engine,
        memory_enabled=True,
        memory_schema=settings.memory_schema,
        memory_collection=settings.memory_collection_name,
    )
    await store.validate_schema()
    queue = _SourceMemoryJobQueue(engine)
    await queue.validate_schema()

    memory_client = create_mem0_client(settings)
    capture = FormationCaptureObserver()
    memory = Mem0Adapter(
        memory_client,
        search_timeout_seconds=settings.memory_search_timeout_seconds,
        operation_timeout_seconds=settings.memory_operation_timeout_seconds,
        observer=capture,
    )
    processor = ProcessMemoryUseCase(
        store,
        memory,
        message_limit=settings.memory_formation_message_limit,
    )
    inspector = PostgresFormationInspector(
        config.memory_database_url,
        schema_name=config.memory_schema,
        collection_name=config.memory_collection,
        runtime_provenance=artifacts.manifest.identity.provenance,
    )
    formation = PersistentFormationRuntime(
        plan=plan,
        ledger=ledger,
        artifacts=artifacts,
        conversation_store=store,
        evaluator=PersistentFormationEvaluator(processor, inspector, capture),
    )

    rewrite_http = httpx.AsyncClient(
        follow_redirects=False, event_hooks=http_measurement_hooks(ProviderStage.REWRITE)
    )
    judge_http = httpx.AsyncClient(follow_redirects=False)
    kira_http = httpx.AsyncClient(follow_redirects=False)
    rewriter = VllmQueryRewriterAdapter(rewrite_http, settings)
    judge = InternalSemanticJudge(judge_http, config)

    def kira_factory(username: str) -> KiraClientPort:
        return KiraHttpAdapter(kira_http, settings.model_copy(update={"kira_username": username}))

    async def failed_source_cleanup(user_id: str, session_ids: Sequence[str]) -> None:
        # Each case/attempt owns its entire user ID. Historical receipts have no conversation
        # column, so owner-scoped deletion also works without changing the frozen schema.
        memory_engine = create_postgres_engine(SimplePostgresSettings(settings.memory_database_url))
        try:
            async with memory_engine.begin() as connection:
                marker = (
                    await connection.execute(
                        text(
                            "SELECT run_id, owner_token, plan_sha256 FROM "
                            f'"{plan.memory_schema}"."benchmark_owner"'
                        )
                    )
                ).one_or_none()
                if marker is None or tuple(marker) != (
                    plan.run_id,
                    plan.owner_token,
                    isolation_plan_sha256(plan),
                ):
                    raise CrossSessionProtocolError
                for table, owner_column in (
                    (f"{settings.memory_collection_name}_formation_receipts", "user_id"),
                    (settings.memory_collection_name, "payload->>'user_id'"),
                    (f"{settings.memory_collection_name}_entities", "payload->>'user_id'"),
                ):
                    await connection.execute(
                        text(
                            f'DELETE FROM "{settings.memory_schema}"."{table}" '
                            f"WHERE {owner_column} = :user_id"
                        ),
                        {"user_id": user_id},
                    )
            async with engine.begin() as connection:
                await connection.execute(
                    delete(conversations).where(
                        conversations.c.user_id == user_id,
                        conversations.c.session_id.in_(session_ids),
                    )
                )
        finally:
            await memory_engine.dispose()

    async def recover_receipted_job(event_id, reference, memory_count) -> None:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE memory_jobs SET status='completed', lease_owner=NULL, "
                    "lease_token=NULL, lease_expires_at=NULL, "
                    "completed_at=COALESCE(completed_at, now()), "
                    "dead_at=NULL, last_error_class=NULL, lifecycle_event_count=:count, "
                    "attempt_count=GREATEST(attempt_count,1), updated_at=now() "
                    "WHERE event_id=:event AND boundary_message_id=:boundary "
                    "AND EXISTS (SELECT 1 FROM conversation_messages m JOIN conversations c "
                    "ON c.conversation_id=m.conversation_id WHERE m.message_id=:boundary "
                    "AND c.conversation_id=:conversation AND c.user_id=:owner)"
                ),
                {
                    "count": memory_count,
                    "event": event_id,
                    "boundary": reference.boundary_message_id,
                    "conversation": reference.conversation_id,
                    "owner": reference.user_id,
                },
            )
            if result.rowcount != 1:
                raise CrossSessionProtocolError

    retrieval_evaluator = RetrievalEvaluator(
        memory,
        profile=config.profile,
        backend="native",
        top_k=config.retrieval_depth,
        threshold=config.retrieval_threshold,
    )
    retrieval = PersistentRetrievalRuntime(
        plan=plan,
        cases=cases,
        client=cast(GoldFixtureClient, memory_client),
        evaluator=retrieval_evaluator,
        formation=formation,
        # Runtime-legacy stores predate the conversation lifecycle ports; their
        # fixtures then run without a conversation owner, like the
        # historical-control receipts, which record only event/owner.
        conversation_store=_has_conversation_lifecycle(store) and store or None,
    )
    process_job = ProcessMemoryJobUseCase(
        processor,
        queue,
        max_attempts=2,
        retry_delays_seconds=(1,),
    )
    gold_fact_text = {fact.gold_id: fact.text for case in cases for fact in case.gold.facts}
    gold_fact_source_ids = {
        fact.gold_id: fact.evidence_message_ids for case in cases for fact in case.gold.facts
    }
    cross_session = NativeCrossSessionRuntime(
        plan=plan,
        ledger=ledger,
        artifacts=artifacts,
        settings=settings,
        conversation_store=store,
        memory=memory,
        memory_queue=queue,
        process_job=process_job,
        inspector=inspector,
        rewriter=rewriter,
        kira_factory=kira_factory,
        judge=judge,
        gold_fact_text=gold_fact_text,
        gold_fact_source_ids=gold_fact_source_ids,
        failed_source_cleanup=failed_source_cleanup,
        recover_receipted_job=recover_receipted_job,
    )
    return NativeRuntimeResources(
        settings=settings,
        engine=engine,
        http_clients=(rewrite_http, judge_http, kira_http),
        memory=memory,
        formation=formation,
        retrieval=retrieval,
        cross_session=cross_session,
        rewriter=rewriter,
        judge=judge,
    )


async def cleanup_native_runtime(
    *,
    config: EvalConfig,
    plan: IsolationPlan,
    ledger: IsolationLedger,
) -> None:
    """Delete exact public rows and drop only the schema with the matching owner marker."""

    await _delete_owned_conversations(config, ledger)
    await asyncio.to_thread(_drop_owned_memory_schema, config, plan, allow_missing=True)


async def _initialize_owned_state(
    config: EvalConfig, plan: IsolationPlan, ledger: IsolationLedger
) -> None:
    exists = await asyncio.to_thread(_owned_schema_exists, config, plan)
    if exists:
        return
    if ledger.resources:
        raise ValueError("owned memory state is missing; resume cannot replay a completed source")
    await _write_owner_marker(config, plan)


def _owned_schema_exists(config: EvalConfig, plan: IsolationPlan) -> bool:
    with psycopg.connect(_memory_dsn(config)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regnamespace(%s)", (plan.memory_schema,))
        exists = cursor.fetchone()
        if not exists or exists[0] is None:
            return False
        try:
            cursor.execute(
                sql.SQL("SELECT run_id, owner_token, plan_sha256 FROM {}").format(
                    sql.Identifier(plan.memory_schema, "benchmark_owner")
                )
            )
            marker = cursor.fetchone()
        except psycopg.errors.UndefinedTable:
            raise ValueError("memory schema has no benchmark owner marker") from None
        if marker != (plan.run_id, plan.owner_token, isolation_plan_sha256(plan)):
            raise ValueError("memory schema benchmark owner marker mismatch")
        return True


async def _delete_owned_conversations(config: EvalConfig, ledger: IsolationLedger) -> None:
    if not ledger.resources:
        return
    if config.database_url is None:
        raise ValueError("native cleanup requires the conversation database")
    settings = SimplePostgresSettings(config.database_url)
    engine = create_postgres_engine(settings)
    try:
        user_ids = tuple(resource.user_id for resource in ledger.resources)
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(conversations.c.user_id.in_(user_ids))
            )
    finally:
        await engine.dispose()


class SimplePostgresSettings:
    """Minimal structural settings accepted by ``create_postgres_engine``."""

    def __init__(self, database_url) -> None:
        self.database_url = database_url
        self.postgres_pool_size = 2
        self.postgres_max_overflow = 0
        self.postgres_pool_timeout_seconds = 2.0
        self.postgres_connect_timeout_seconds = 2.0
        self.postgres_command_timeout_seconds = 5.0


async def _write_owner_marker(config: EvalConfig, plan: IsolationPlan) -> None:
    await asyncio.to_thread(_write_owner_marker_sync, config, plan)


def _memory_dsn(config: EvalConfig) -> str:
    if config.memory_database_url is None:
        raise ValueError("native runtime requires the memory database")
    return normalize_psycopg_dsn(config.memory_database_url.get_secret_value())


def _write_owner_marker_sync(config: EvalConfig, plan: IsolationPlan) -> None:
    with psycopg.connect(_memory_dsn(config)) as connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(plan.memory_schema)))
        cursor.execute(
            sql.SQL(
                "CREATE TABLE {} (singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton), "
                "run_id UUID NOT NULL, owner_token UUID NOT NULL, plan_sha256 TEXT NOT NULL)"
            ).format(sql.Identifier(plan.memory_schema, "benchmark_owner"))
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO {} (singleton, run_id, owner_token, plan_sha256) "
                "VALUES (TRUE, %s, %s, %s)"
            ).format(sql.Identifier(plan.memory_schema, "benchmark_owner")),
            (plan.run_id, plan.owner_token, isolation_plan_sha256(plan)),
        )


def _drop_owned_memory_schema(
    config: EvalConfig,
    plan: IsolationPlan,
    allow_missing: bool = False,
) -> None:
    with psycopg.connect(_memory_dsn(config)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regnamespace(%s)", (plan.memory_schema,))
        exists = cursor.fetchone()
        if not exists or exists[0] is None:
            if allow_missing:
                return
            raise ValueError("owned memory schema is missing")
        try:
            cursor.execute(
                sql.SQL("SELECT run_id, owner_token, plan_sha256 FROM {}").format(
                    sql.Identifier(plan.memory_schema, "benchmark_owner")
                )
            )
            marker = cursor.fetchone()
        except psycopg.errors.UndefinedTable:
            raise ValueError("memory schema has no benchmark owner marker") from None
        expected = (plan.run_id, plan.owner_token, isolation_plan_sha256(plan))
        if marker != expected:
            raise ValueError("memory schema benchmark owner marker mismatch")
        cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(plan.memory_schema)))
