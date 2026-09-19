"""In-process native runtime used by company-PC and internal benchmark profiles.

This composition reuses the production application ports and adapters.  It intentionally does not
add a benchmark endpoint to the Gateway.  The benchmark process owns a run-scoped memory schema,
run-scoped identities and a stopped-worker queue consumer so No-LTM/With-LTM arms cannot mutate
global runtime flags or race another worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import sql
from sqlalchemy import delete
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
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraStreamEvent
from app.domain.models.memory import LongTermMemory
from app.domain.models.memory_job import MemoryJob
from app.domain.ports.context_observer import StageName
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
from app.infrastructure.postgres.schema import conversations
from evaluation.artifacts import (
    ArtifactStore,
    CaseAttemptArtifact,
    DiagnosticArtifact,
    FormedCorpusArtifact,
)
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
)
from evaluation.isolation import (
    IsolationLedger,
    IsolationPlan,
    allocate_case_resources,
    claim_case_resources,
    isolation_plan_sha256,
    register_case_resources,
    register_memory_ids,
)
from evaluation.judge import InternalSemanticJudge, JudgeError
from evaluation.models import CrossSessionInput, EvalCase, FormationInput, Outcome, RetrievalInput
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
from evaluation.scoring import score_formation

_FALLBACK_TIME = datetime(2000, 1, 1, tzinfo=UTC)


def reconcile_interrupted_attempts(
    artifacts: ArtifactStore,
    cases: Sequence[EvalCase],
) -> None:
    """Close a durably allocated attempt that crashed before its case artifact was appended."""

    ledger = artifacts.load_isolation_ledger()
    case_by_id = {case.case_id: case for case in cases}
    for resource in ledger.resources:
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
        name: StageName,
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

    async def search(
        self, user_id: str, query: str, *, top_k: int, threshold: float
    ) -> tuple[LongTermMemory, ...]:
        if self._memory is None:
            self.returned = ()
        else:
            self.returned = await self._memory.search(
                user_id,
                query,
                top_k=top_k,
                threshold=threshold,
            )
        return self.returned

    async def process_memory(self, source: object):
        if self._memory is None:  # pragma: no cover - never used by Session B
            raise RuntimeError("memory disabled")
        return await self._memory.process_memory(source)  # type: ignore[arg-type]


class _RecordingRewriter(QueryRewriterPort):
    def __init__(self, rewriter: QueryRewriterPort) -> None:
        self._rewriter = rewriter
        self.output: str | None = None

    async def rewrite(self, context) -> str:
        self.output = await self._rewriter.rewrite(context)
        return self.output


class _RecordingKira(KiraClientPort):
    def __init__(self, client: KiraClientPort) -> None:
        self._client = client
        self.messages: list[str] = []
        self.events: list[KiraStreamEvent] = []

    async def chat_stream(self, message: str) -> AsyncIterator[KiraStreamEvent]:
        self.messages.append(message)
        source = await self._client.chat_stream(message)

        async def recorded() -> AsyncIterator[KiraStreamEvent]:
            async for event in source:
                self.events.append(event)
                yield event

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
    ) -> None:
        self._cases = tuple(case for case in cases if isinstance(case.inputs, RetrievalInput))
        self._evaluator = evaluator
        self._formation = formation
        self._gold_manager = GoldRetrievalFixtureManager(client, plan)
        self._formed_manager = FormationRetrievalFixtureManager(client, plan)
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


class NativeCrossSessionRuntime(CrossSessionRuntimePort):
    """Own one exact queue delivery and two isolated Session-B application executions."""

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
        kira: KiraHttpAdapter,
        judge: InternalSemanticJudge,
        gold_fact_text: Mapping[str, str],
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
        self._kira = kira
        self._judge = judge
        self._gold_fact_text = dict(gold_fact_text)
        self._pending: dict[UUID, tuple[EvalCase, MemoryJob, str]] = {}

    async def persist_session_a(self, case: EvalCase) -> MemoryJobHandle:
        if not isinstance(case.inputs, CrossSessionInput):
            raise ValueError("cross-session runtime accepts cross-session cases only")
        attempt = self.artifacts.next_attempt_number(case.case_id)
        self.ledger = self.artifacts.load_isolation_ledger()
        allocated = allocate_case_resources(self.plan, case_id=case.case_id, attempt=attempt)
        self.ledger = register_case_resources(self.ledger, self.plan, allocated)
        self.artifacts.write_isolation_ledger(self.ledger)
        reference = await _persist_message_pairs(
            self._store,
            user_id=allocated.user_id,
            session_id=allocated.session_id,
            messages=case.inputs.session_a_messages,
            schedule_last=True,
        )
        jobs = await self._queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=max(self._settings.memory_operation_timeout_seconds + 10, 30),
            max_attempts=1,
        )
        if len(jobs) != 1 or jobs[0].reference != reference:
            raise CrossSessionProtocolError
        job = jobs[0]
        resource = claim_case_resources(
            self.plan,
            case_id=case.case_id,
            attempt=attempt,
            conversation_id=reference.conversation_id,
            event_id=job.event_id,
        )
        self.ledger = register_case_resources(self.ledger, self.plan, resource)
        self.artifacts.write_isolation_ledger(self.ledger)
        self._pending[job.event_id] = (case, job, allocated.user_id)
        return MemoryJobHandle(event_id=job.event_id)

    async def wait_for_memory(
        self,
        handle: MemoryJobHandle,
        *,
        timeout_seconds: float,
    ) -> MemoryReadiness:
        del timeout_seconds
        pending = self._pending.pop(handle.event_id, None)
        if pending is None:
            raise CrossSessionProtocolError
        case, job, persisted_user = pending
        result = await self._process_job.execute(job)
        if result.outcome is MemoryJobProcessOutcome.DEAD:
            return MemoryReadiness(event_id=handle.event_id, status=MemoryReadinessStatus.DEAD)
        if result.outcome is not MemoryJobProcessOutcome.COMPLETED:
            raise CrossSessionDependencyError
        snapshot = await self._inspector.inspect(
            event_id=handle.event_id,
            user_id=persisted_user,
        )
        if snapshot.receipt is None:
            raise CrossSessionProtocolError
        mapping = await self._map_gold(case, snapshot.receipt.events)
        memory_ids = tuple(memory.memory_id for memory in snapshot.memories)
        resource = next(
            item
            for item in self.ledger.resources
            if item.case_id == case.case_id
            and item.attempt == self.artifacts.next_attempt_number(case.case_id)
        )
        self.ledger = register_memory_ids(
            self.ledger,
            self.plan,
            case_id=resource.case_id,
            attempt=resource.attempt,
            memory_ids=memory_ids,
        )
        self.artifacts.write_isolation_ledger(self.ledger)
        return MemoryReadiness(
            event_id=handle.event_id,
            status=MemoryReadinessStatus.COMPLETED,
            gold_ids_by_memory_id=mapping,
        )

    async def _map_gold(self, case: EvalCase, events) -> dict[str, tuple[str, ...]]:
        relevant = {
            gold_id: self._gold_fact_text[gold_id]
            for gold_id in case.gold.relevant_memory_ids
            if gold_id in self._gold_fact_text
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
        return {
            str(event.memory_id): ((by_prediction[index],) if index in by_prediction else ())
            for index, event in enumerate(events)
        }

    async def run_session_b(
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
        kira = _RecordingKira(self._kira)
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
        actual_session = f"{resource.session_id}:{condition.value}"
        session = await use_case.execute(
            ChatCommand(session_id=actual_session, message=case.inputs.session_b_query),
            principal=AuthenticatedPrincipal(user_id=resource.user_id),
            correlation_id=uuid4().hex,
            turn_id=f"{case.case_id}:{condition.value}",
        )
        async for _ in session:
            pass
        if (
            not session.source_exhausted
            or not session.final_text.strip()
            or len(kira.messages) != 1
        ):
            raise CrossSessionProtocolError
        rewritten = rewriter.output or case.inputs.session_b_query
        return SessionBExecution(
            condition=condition,
            user_id=case.inputs.user_id,
            session_id=case.inputs.session_b,
            current_query=case.inputs.session_b_query,
            provider_id=f"kira-service-{self._settings.kira_service_id}",
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


async def _persist_message_pairs(
    store: PostgresConversationStoreAdapter,
    *,
    user_id: str,
    session_id: str,
    messages: Sequence,
    schedule_last: bool,
):
    if not messages or len(messages) % 2:
        raise ValueError("native formation needs complete user/assistant pairs")
    reference = None
    for index in range(0, len(messages), 2):
        user, assistant = messages[index : index + 2]
        if user.role != "user" or assistant.role != "assistant":
            raise ValueError("native formation messages must be ordered pairs")
        turn_id = user.message_id
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

    settings = settings_for_native_runtime(base_settings, config)
    await _reset_owned_state(config, plan, ledger)
    await _write_owner_marker(config, plan)
    await initialize_memory_schema(settings)
    engine = create_postgres_engine(settings)
    store = PostgresConversationStoreAdapter(engine)
    await store.validate_schema()
    queue = PostgresMemoryJobQueueAdapter(engine)
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
    )
    formation = PersistentFormationRuntime(
        plan=plan,
        ledger=ledger,
        artifacts=artifacts,
        conversation_store=store,
        evaluator=PersistentFormationEvaluator(processor, inspector, capture),
    )

    rewrite_http = httpx.AsyncClient(follow_redirects=False)
    judge_http = httpx.AsyncClient(follow_redirects=False)
    kira_http = httpx.AsyncClient(follow_redirects=False)
    rewriter = VllmQueryRewriterAdapter(rewrite_http, settings)
    judge = InternalSemanticJudge(judge_http, config)
    kira = KiraHttpAdapter(kira_http, settings)
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
    )
    process_job = ProcessMemoryJobUseCase(
        processor,
        queue,
        max_attempts=1,
        retry_delays_seconds=(),
    )
    gold_fact_text = {fact.gold_id: fact.text for case in cases for fact in case.gold.facts}
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
        kira=kira,
        judge=judge,
        gold_fact_text=gold_fact_text,
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
    await asyncio.to_thread(_drop_owned_memory_schema, config, plan)


async def _reset_owned_state(
    config: EvalConfig,
    plan: IsolationPlan,
    ledger: IsolationLedger,
) -> None:
    """Make a resume deterministic after validating any existing run-owned schema marker."""

    await _delete_owned_conversations(config, ledger)
    await asyncio.to_thread(_drop_owned_memory_schema, config, plan, True)


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
