"""Run-scoped retrieval fixtures and evaluators.

Gold fixtures are written through Mem0's public ``add(..., infer=False)`` boundary.  This keeps
the native embedding/vector-store path while deliberately bypassing memory extraction.  Cleanup
is restricted to the exact memory IDs returned during setup; this module never patches runtime
tables or issues broad user-scoped deletes.
"""

import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.domain.errors.memory import (
    LongTermMemoryConnectionError,
    LongTermMemoryOperationError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.models.conversation import (
    AppendTurnResult,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import LongTermMemory
from app.domain.ports.long_term_memory import LongTermMemoryPort
from app.infrastructure.memory.mem0_adapter import Mem0Adapter
from evaluation.artifacts import FormedCorpusArtifact
from evaluation.isolation import IsolationPlan
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    EvalCase,
    EvalModel,
    Identifier,
    NonEmpty,
    Outcome,
    Profile,
    RetrievalInput,
    SeedMemory,
)
from evaluation.scoring import RetrievalScore, score_retrieval_groups


class GoldFixtureClient(Protocol):
    """Small public Mem0 surface required to own a disposable gold corpus."""

    async def add(self, messages: object, **kwargs: Any) -> object: ...

    async def delete(self, memory_id: str) -> object: ...


class RetrievalFixtureConversationStore(Protocol):
    """Conversation lifecycle surface needed to give fixture memories a real owner."""

    async def append_turn(
        self,
        user_id: str,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        *,
        schedule_memory: bool = False,
    ) -> AppendTurnResult: ...


class _FixtureOwner:
    """Own one fixture conversation through the shared append-turn boundary."""

    def __init__(self, store, *, title: str) -> None:
        self._store = store
        self._title = title
        self._session_ids: dict[str, str] = {}
        self._conversation_ids: dict[str, UUID] = {}

    async def conversation_id(self, user_id: str) -> UUID | None:
        if self._store is None:
            return None
        existing = self._conversation_ids.get(user_id)
        if existing is None:
            session_id = f"eval-{uuid4().hex[:12]}"
            turn_id = f"fixture-{uuid4().hex[:12]}"
            appended = await self._store.append_turn(
                user_id,
                ConversationMessage(
                    session_id,
                    turn_id,
                    ConversationRole.USER,
                    self._title,
                    datetime.now(UTC),
                ),
                ConversationMessage(
                    session_id,
                    turn_id,
                    ConversationRole.ASSISTANT,
                    self._title,
                    datetime.now(UTC),
                ),
            )
            self._session_ids[user_id] = appended.reference.session_id
            self._conversation_ids[user_id] = appended.reference.conversation_id
            return appended.reference.conversation_id
        return existing

    async def retire(self) -> bool:
        if self._store is None:
            return False
        failed = False
        for user_id, session_id in reversed(tuple(self._session_ids.items())):
            try:
                if not await self._store.mark_deletion_pending(user_id, session_id):
                    failed = True
                elif not await self._store.purge_deletion_pending(user_id, session_id):
                    failed = True
            except Exception:
                failed = True
        self._session_ids.clear()
        self._conversation_ids.clear()
        return failed


class GoldFixtureError(RuntimeError):
    """Content-free fixture failure safe to persist as a dependency diagnostic."""


class GoldFixtureMemory(EvalModel):
    gold_id: Identifier
    logical_user_id: Identifier
    persisted_user_id: Identifier
    memory_id: UUID
    content: NonEmpty


class RetrievalFixtureUserScope(EvalModel):
    logical_user_id: Identifier
    persisted_user_id: Identifier


class GoldRetrievalFixture(EvalModel):
    """Exact mapping between canonical gold IDs and run-local backend IDs."""

    schema_version: Literal[1] = 1
    run_id: UUID
    owner_token: UUID
    memory_schema: str = Field(pattern=r"^eval_[a-f0-9]{12}$")
    memory_collection: str = Field(pattern=r"^mem_[a-f0-9]{12}$")
    case_ids: tuple[Identifier, ...]
    user_scopes: tuple[RetrievalFixtureUserScope, ...]
    memories: tuple[GoldFixtureMemory, ...]

    @model_validator(mode="after")
    def fixture_is_closed_and_unambiguous(self) -> "GoldRetrievalFixture":
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("gold fixture case IDs must be unique")
        memory_ids = [memory.memory_id for memory in self.memories]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("gold fixture backend memory IDs must be unique")
        gold_keys = [(memory.logical_user_id, memory.gold_id) for memory in self.memories]
        if len(gold_keys) != len(set(gold_keys)):
            raise ValueError("gold fixture memories must be unique per logical user")
        scope_by_logical = _validate_user_scopes(self.user_scopes)
        for memory in self.memories:
            if scope_by_logical.get(memory.logical_user_id) != memory.persisted_user_id:
                raise ValueError("gold fixture memory is outside its declared user scope")
        return self

    def persisted_user_id(self, logical_user_id: str) -> str:
        users = {
            scope.persisted_user_id
            for scope in self.user_scopes
            if scope.logical_user_id == logical_user_id
        }
        if len(users) != 1:  # pragma: no cover - model validation prevents ambiguity
            raise KeyError("logical user has no unique gold fixture scope")
        return users.pop()

    def gold_id_by_memory_id(self) -> dict[str, str]:
        return {str(memory.memory_id): memory.gold_id for memory in self.memories}


def _persisted_fixture_user(plan: IsolationPlan, logical_user_id: str) -> str:
    digest = hashlib.sha256(f"{plan.owner_token}\0{logical_user_id}".encode()).hexdigest()[:16]
    return f"{plan.user_namespace}:gold:{digest}"


def _gold_memories(cases: Sequence[EvalCase]) -> tuple[SeedMemory, ...]:
    by_key: dict[tuple[str, str], SeedMemory] = {}
    for case in cases:
        if not isinstance(case.inputs, RetrievalInput):
            raise ValueError("gold retrieval fixture accepts retrieval cases only")
        for memory in case.inputs.memories:
            if memory.user_id != case.inputs.user_id:
                raise ValueError("gold memory owner differs from its retrieval case")
            key = (memory.user_id, memory.gold_id)
            existing = by_key.setdefault(key, memory)
            if existing != memory:
                raise ValueError("gold memory ID has conflicting canonical content")
    return tuple(by_key[key] for key in sorted(by_key))


def _parse_add_result(response: object, expected_content: str) -> UUID:
    try:
        if not isinstance(response, Mapping):
            raise ValueError
        rows = response["results"]
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError
        row = rows[0]
        if (
            not isinstance(row, Mapping)
            or row.get("event") != "ADD"
            or row.get("memory") != expected_content
            or not isinstance(row.get("id"), str)
        ):
            raise ValueError
        return UUID(row["id"])
    except (KeyError, TypeError, ValueError):
        raise GoldFixtureError("gold_fixture_add_protocol_error") from None


class GoldRetrievalFixtureManager:
    """Create and remove an exact gold corpus owned by one isolation plan."""

    def __init__(
        self,
        client: GoldFixtureClient,
        plan: IsolationPlan,
        conversation_store: RetrievalFixtureConversationStore | None = None,
    ) -> None:
        self._client = client
        self._plan = plan
        self._owners = _FixtureOwner(conversation_store, title="evaluation:gold-retrieval")

    async def setup(self, cases: Sequence[EvalCase]) -> GoldRetrievalFixture:
        case_ids = tuple(case.case_id for case in cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("gold fixture retrieval cases must be unique")
        seeds = _gold_memories(cases)
        logical_users = sorted(self._logical_users(cases))
        created: list[GoldFixtureMemory] = []
        try:
            for seed in seeds:
                persisted_user_id = _persisted_fixture_user(self._plan, seed.user_id)
                conversation_id = await self._owners.conversation_id(persisted_user_id)
                metadata = {
                    "eval_fixture": "gold_retrieval",
                    "eval_run_id": str(self._plan.run_id),
                    "gold_memory_id": seed.gold_id,
                    "logical_user_id": seed.user_id,
                }
                if conversation_id is not None:
                    metadata["conversation_id"] = str(conversation_id)
                response = await self._client.add(
                    [{"role": "user", "content": seed.text}],
                    user_id=persisted_user_id,
                    metadata=metadata,
                    infer=False,
                )
                created.append(
                    GoldFixtureMemory(
                        gold_id=seed.gold_id,
                        logical_user_id=seed.user_id,
                        persisted_user_id=persisted_user_id,
                        memory_id=_parse_add_result(response, seed.text),
                        content=seed.text,
                    )
                )
        except BaseException as error:
            await self._rollback(created)
            await self._owners.retire()
            if not isinstance(error, Exception):
                raise
            if isinstance(error, GoldFixtureError):
                raise
            raise GoldFixtureError("gold_fixture_setup_failed") from None
        return GoldRetrievalFixture(
            run_id=self._plan.run_id,
            owner_token=self._plan.owner_token,
            memory_schema=self._plan.memory_schema,
            memory_collection=self._plan.memory_collection,
            case_ids=case_ids,
            user_scopes=tuple(
                RetrievalFixtureUserScope(
                    logical_user_id=user_id,
                    persisted_user_id=_persisted_fixture_user(self._plan, user_id),
                )
                for user_id in logical_users
            ),
            memories=tuple(created),
        )

    async def cleanup(self, fixture: GoldRetrievalFixture) -> None:
        self._validate_owner(fixture)
        failed = await self._delete_exact(fixture.memories)
        owner_failed = await self._owners.retire()
        if failed or owner_failed:
            raise GoldFixtureError("gold_fixture_cleanup_failed")

    async def _rollback(self, memories: Sequence[GoldFixtureMemory]) -> None:
        await self._delete_exact(memories)

    async def _delete_exact(self, memories: Sequence[GoldFixtureMemory]) -> bool:
        failed = False
        for memory in reversed(memories):
            try:
                await self._client.delete(str(memory.memory_id))
            except Exception:
                failed = True
        return failed

    def _validate_owner(self, fixture: GoldRetrievalFixture) -> None:
        plan = self._plan
        if (
            fixture.run_id != plan.run_id
            or fixture.owner_token != plan.owner_token
            or fixture.memory_schema != plan.memory_schema
            or fixture.memory_collection != plan.memory_collection
        ):
            raise ValueError("gold fixture does not belong to this isolation plan")

    @staticmethod
    def _logical_users(cases: Sequence[EvalCase]) -> set[str]:
        users: set[str] = set()
        for case in cases:
            if not isinstance(case.inputs, RetrievalInput):
                raise ValueError("gold retrieval fixture accepts retrieval cases only")
            users.add(case.inputs.user_id)
        return users


class RetrievalMode(StrEnum):
    GOLD_FIXTURE = "gold_fixture"
    FORMATION_PRODUCED = "formation_produced"


class FormedFixtureMemory(EvalModel):
    source_gold_ids: tuple[Identifier, ...]
    logical_user_id: Identifier
    persisted_user_id: Identifier
    source_memory_id: UUID
    memory_id: UUID
    content: NonEmpty

    @model_validator(mode="after")
    def gold_ids_are_unique(self) -> "FormedFixtureMemory":
        if len(self.source_gold_ids) != len(set(self.source_gold_ids)):
            raise ValueError("formed fixture source gold IDs must be unique")
        return self


class FormationRetrievalFixture(EvalModel):
    """Run-local retrieval copy of the exact contents produced by formation."""

    schema_version: Literal[1] = 1
    run_id: UUID
    owner_token: UUID
    memory_schema: str = Field(pattern=r"^eval_[a-f0-9]{12}$")
    memory_collection: str = Field(pattern=r"^mem_[a-f0-9]{12}$")
    case_ids: tuple[Identifier, ...]
    user_scopes: tuple[RetrievalFixtureUserScope, ...]
    memories: tuple[FormedFixtureMemory, ...]

    @model_validator(mode="after")
    def fixture_is_closed_and_unambiguous(self) -> "FormationRetrievalFixture":
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("formation retrieval fixture case IDs must be unique")
        memory_ids = [memory.memory_id for memory in self.memories]
        source_ids = [memory.source_memory_id for memory in self.memories]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("formation fixture backend memory IDs must be unique")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("formation fixture source memory IDs must be unique")
        scope_by_logical = _validate_user_scopes(self.user_scopes)
        for memory in self.memories:
            if scope_by_logical.get(memory.logical_user_id) != memory.persisted_user_id:
                raise ValueError("formation fixture memory is outside its declared user scope")
        return self

    def persisted_user_id(self, logical_user_id: str) -> str:
        users = {
            scope.persisted_user_id
            for scope in self.user_scopes
            if scope.logical_user_id == logical_user_id
        }
        if len(users) != 1:  # pragma: no cover - model validation prevents ambiguity
            raise KeyError("logical user has no unique formation fixture scope")
        return users.pop()

    def gold_ids_by_memory_id(self) -> dict[str, tuple[str, ...]]:
        return {str(memory.memory_id): memory.source_gold_ids for memory in self.memories}


class FormationRetrievalFixtureManager:
    """Materialize formation outputs without rerunning extraction or substituting gold text."""

    def __init__(
        self,
        client: GoldFixtureClient,
        plan: IsolationPlan,
        conversation_store: RetrievalFixtureConversationStore | None = None,
    ) -> None:
        self._client = client
        self._plan = plan
        self._owners = _FixtureOwner(conversation_store, title="evaluation:formed-retrieval")

    async def setup(
        self,
        corpus: FormedCorpusArtifact,
        retrieval_cases: Sequence[EvalCase],
    ) -> FormationRetrievalFixture:
        if corpus.identity.run_id != self._plan.run_id:
            raise ValueError("formed corpus belongs to another benchmark run")
        case_ids = tuple(case.case_id for case in retrieval_cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("formation retrieval cases must be unique")
        if any(not isinstance(case.inputs, RetrievalInput) for case in retrieval_cases):
            raise ValueError("formation retrieval fixture accepts retrieval cases only")
        logical_users = {case.inputs.user_id for case in retrieval_cases}
        created: list[FormedFixtureMemory] = []
        try:
            for memory in corpus.memories:
                if memory.logical_user_id not in logical_users:
                    continue
                persisted_user_id = _persisted_formed_user(
                    self._plan,
                    memory.logical_user_id,
                )
                conversation_id = await self._owners.conversation_id(persisted_user_id)
                metadata = {
                    "eval_fixture": "formation_produced",
                    "eval_run_id": str(self._plan.run_id),
                    "source_memory_id": str(memory.memory_id),
                    "source_gold_ids": list(memory.source_gold_ids),
                    "logical_user_id": memory.logical_user_id,
                }
                if conversation_id is not None:
                    metadata["conversation_id"] = str(conversation_id)
                response = await self._client.add(
                    [{"role": "user", "content": memory.content}],
                    user_id=persisted_user_id,
                    metadata=metadata,
                    infer=False,
                )
                created.append(
                    FormedFixtureMemory(
                        source_gold_ids=memory.source_gold_ids,
                        logical_user_id=memory.logical_user_id,
                        persisted_user_id=persisted_user_id,
                        source_memory_id=memory.memory_id,
                        memory_id=_parse_add_result(response, memory.content),
                        content=memory.content,
                    )
                )
        except BaseException as error:
            await self._delete_exact(created)
            await self._owners.retire()
            if not isinstance(error, Exception):
                raise
            if isinstance(error, GoldFixtureError):
                raise
            raise GoldFixtureError("formation_fixture_setup_failed") from None
        return FormationRetrievalFixture(
            run_id=self._plan.run_id,
            owner_token=self._plan.owner_token,
            memory_schema=self._plan.memory_schema,
            memory_collection=self._plan.memory_collection,
            case_ids=case_ids,
            user_scopes=tuple(
                RetrievalFixtureUserScope(
                    logical_user_id=user_id,
                    persisted_user_id=_persisted_formed_user(self._plan, user_id),
                )
                for user_id in sorted(logical_users)
            ),
            memories=tuple(created),
        )

    async def cleanup(self, fixture: FormationRetrievalFixture) -> None:
        self._validate_owner(fixture)
        failed = await self._delete_exact(fixture.memories)
        owner_failed = await self._owners.retire()
        if failed or owner_failed:
            raise GoldFixtureError("formation_fixture_cleanup_failed")

    async def _delete_exact(self, memories: Sequence[FormedFixtureMemory]) -> bool:
        failed = False
        for memory in reversed(memories):
            try:
                await self._client.delete(str(memory.memory_id))
            except Exception:
                failed = True
        return failed

    def _validate_owner(self, fixture: FormationRetrievalFixture) -> None:
        plan = self._plan
        if (
            fixture.run_id != plan.run_id
            or fixture.owner_token != plan.owner_token
            or fixture.memory_schema != plan.memory_schema
            or fixture.memory_collection != plan.memory_collection
        ):
            raise ValueError("formation fixture does not belong to this isolation plan")


def _persisted_formed_user(plan: IsolationPlan, logical_user_id: str) -> str:
    digest = hashlib.sha256(f"{plan.owner_token}\0formed\0{logical_user_id}".encode()).hexdigest()[
        :16
    ]
    return f"{plan.user_namespace}:formed:{digest}"


def _validate_user_scopes(
    scopes: Sequence[RetrievalFixtureUserScope],
) -> dict[str, str]:
    logical_ids = [scope.logical_user_id for scope in scopes]
    persisted_ids = [scope.persisted_user_id for scope in scopes]
    if len(logical_ids) != len(set(logical_ids)):
        raise ValueError("retrieval fixture logical user scopes must be unique")
    if len(persisted_ids) != len(set(persisted_ids)):
        raise ValueError("retrieval fixture persisted user scopes must be unique")
    return {scope.logical_user_id: scope.persisted_user_id for scope in scopes}


class RetrievalCaseEvaluation(EvalModel):
    case_id: Identifier
    mode: RetrievalMode
    outcome: Outcome
    score: RetrievalScore | None = None
    returned_memory_ids: tuple[UUID, ...] = ()
    reason_codes: tuple[Identifier, ...] = ()
    safety_violation_codes: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def state_is_consistent(self) -> "RetrievalCaseEvaluation":
        scored = self.score is not None
        if scored != (self.outcome in (Outcome.PASS, Outcome.FAIL)):
            if not (self.outcome is Outcome.FAIL and self.safety_violation_codes):
                raise ValueError("retrieval quality outcomes require a score")
        if self.safety_violation_codes and self.outcome is not Outcome.FAIL:
            raise ValueError("retrieval safety violations are hard failures")
        if self.safety_violation_codes and self.score is not None:
            raise ValueError("retrieval safety failures cannot receive quality credit")
        for values in (self.reason_codes, self.safety_violation_codes):
            if len(values) != len(set(values)):
                raise ValueError("retrieval result codes must be unique")
        return self


class RetrievalMeanMetric(EvalModel):
    name: Literal["recall_at_3", "mrr_at_10"]
    value: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    total: float = Field(ge=0, allow_inf_nan=False)
    denominator: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def value_matches_denominator(self) -> "RetrievalMeanMetric":
        expected = self.total / self.denominator if self.denominator else None
        if self.value != expected:
            raise ValueError("retrieval mean value does not match its denominator")
        return self


class RetrievalModeReport(EvalModel):
    mode: RetrievalMode
    attempted_cases: int = Field(ge=0, strict=True)
    scored_cases: int = Field(ge=0, strict=True)
    outcomes: dict[Outcome, int]
    recall_at_3: RetrievalMeanMetric
    mrr_at_10: RetrievalMeanMetric
    safety_failure_cases: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "RetrievalModeReport":
        if self.scored_cases > self.attempted_cases:
            raise ValueError("retrieval report counts are inconsistent")
        if sum(self.outcomes.values()) != self.attempted_cases:
            raise ValueError("retrieval outcomes must add up to attempted cases")
        return self


class RetrievalReport(EvalModel):
    schema_version: Literal[1] = 1
    contract_id: Literal["kira-week5-benchmark-v5"] = BENCHMARK_CONTRACT_ID
    gold_fixture: RetrievalModeReport
    formation_produced: RetrievalModeReport

    @model_validator(mode="after")
    def modes_are_separate(self) -> "RetrievalReport":
        if (
            self.gold_fixture.mode is not RetrievalMode.GOLD_FIXTURE
            or self.formation_produced.mode is not RetrievalMode.FORMATION_PRODUCED
        ):
            raise ValueError("retrieval report modes are not separated")
        return self


class RetrievalEvaluator:
    """Search-only evaluator; it has no conversation or formation queue dependency."""

    def __init__(
        self,
        memory: LongTermMemoryPort,
        *,
        profile: Profile,
        backend: Literal["native", "mock"],
        top_k: int = 10,
        threshold: float = 0,
    ) -> None:
        if profile is Profile.INTERNAL_TEST and backend != "native":
            raise ValueError("official internal retrieval requires the native adapter")
        if backend == "native" and not isinstance(memory, Mem0Adapter):
            raise ValueError("native retrieval must use Mem0Adapter")
        if top_k != 10:
            raise ValueError("retrieval evaluator depth is locked to 10")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("retrieval threshold must be numeric")
        if not 0 <= threshold <= 1:
            raise ValueError("retrieval threshold must be between zero and one")
        self._memory = memory
        self._top_k = top_k
        self._threshold = float(threshold)

    async def evaluate_gold_fixture(
        self,
        case: EvalCase,
        fixture: GoldRetrievalFixture | None,
    ) -> RetrievalCaseEvaluation:
        self._inputs(case)
        if case.eligibility.status == "blocked":
            return self._failure(
                case,
                RetrievalMode.GOLD_FIXTURE,
                Outcome.NOT_RUN,
                "retrieval_case_blocked",
            )
        if fixture is None:
            return self._failure(
                case,
                RetrievalMode.GOLD_FIXTURE,
                Outcome.DEPENDENCY_ERROR,
                "gold_fixture_unavailable",
            )
        if case.case_id not in fixture.case_ids:
            return self._failure(
                case,
                RetrievalMode.GOLD_FIXTURE,
                Outcome.DEPENDENCY_ERROR,
                "gold_fixture_case_missing",
            )
        try:
            user_id = fixture.persisted_user_id(self._inputs(case).user_id)
        except KeyError:
            return self._failure(
                case,
                RetrievalMode.GOLD_FIXTURE,
                Outcome.DEPENDENCY_ERROR,
                "gold_fixture_user_missing",
            )
        mapping = {
            memory_id: (gold_id,) for memory_id, gold_id in fixture.gold_id_by_memory_id().items()
        }
        return await self._evaluate(case, RetrievalMode.GOLD_FIXTURE, user_id, mapping)

    async def evaluate_formation_produced(
        self,
        case: EvalCase,
        fixture: FormationRetrievalFixture | None,
    ) -> RetrievalCaseEvaluation:
        self._inputs(case)
        if case.eligibility.status == "blocked":
            return self._failure(
                case,
                RetrievalMode.FORMATION_PRODUCED,
                Outcome.NOT_RUN,
                "retrieval_case_blocked",
            )
        if fixture is None:
            return self._failure(
                case,
                RetrievalMode.FORMATION_PRODUCED,
                Outcome.DEPENDENCY_ERROR,
                "formed_corpus_unavailable",
            )
        if case.case_id not in fixture.case_ids:
            return self._failure(
                case,
                RetrievalMode.FORMATION_PRODUCED,
                Outcome.DEPENDENCY_ERROR,
                "formed_corpus_case_missing",
            )
        try:
            user_id = fixture.persisted_user_id(self._inputs(case).user_id)
        except KeyError:
            return self._failure(
                case,
                RetrievalMode.FORMATION_PRODUCED,
                Outcome.DEPENDENCY_ERROR,
                "formed_corpus_user_missing",
            )
        return await self._evaluate(
            case,
            RetrievalMode.FORMATION_PRODUCED,
            user_id,
            fixture.gold_ids_by_memory_id(),
        )

    async def _evaluate(
        self,
        case: EvalCase,
        mode: RetrievalMode,
        persisted_user_id: str,
        gold_ids_by_memory_id: Mapping[str, tuple[str, ...]],
    ) -> RetrievalCaseEvaluation:
        inputs = self._inputs(case)
        try:
            returned = await self._memory.search(
                persisted_user_id,
                inputs.current_query,
                top_k=self._top_k,
                threshold=self._threshold,
            )
        except (LongTermMemoryConnectionError, LongTermMemoryTimeoutError):
            return self._failure(case, mode, Outcome.DEPENDENCY_ERROR, "retrieval_unavailable")
        except LongTermMemoryOperationError:
            return self._failure(case, mode, Outcome.DEPENDENCY_ERROR, "retrieval_provider_error")
        except LongTermMemoryProtocolError:
            return self._failure(case, mode, Outcome.PROTOCOL_ERROR, "retrieval_protocol_error")
        except Exception:
            return self._failure(case, mode, Outcome.PROTOCOL_ERROR, "retrieval_unexpected_error")
        if len(returned) > self._top_k:
            return self._failure(case, mode, Outcome.PROTOCOL_ERROR, "retrieval_depth_exceeded")

        try:
            returned_ids = tuple(UUID(memory.memory_id) for memory in returned)
        except ValueError:
            return self._failure(case, mode, Outcome.PROTOCOL_ERROR, "retrieval_memory_id_invalid")
        unique = _unique_memories(returned)
        if any(
            (owner := memory.metadata.get("user_id")) is not None and owner != persisted_user_id
            for memory in unique
        ):
            return RetrievalCaseEvaluation(
                case_id=case.case_id,
                mode=mode,
                outcome=Outcome.FAIL,
                returned_memory_ids=returned_ids,
                safety_violation_codes=("retrieval_cross_user_result",),
            )
        if any(memory.memory_id not in gold_ids_by_memory_id for memory in unique):
            return RetrievalCaseEvaluation(
                case_id=case.case_id,
                mode=mode,
                outcome=Outcome.FAIL,
                returned_memory_ids=returned_ids,
                safety_violation_codes=("retrieval_foreign_corpus_result",),
            )
        score = score_retrieval_groups(
            case.gold.relevant_memory_ids,
            tuple(gold_ids_by_memory_id[memory.memory_id] for memory in unique),
        )
        return RetrievalCaseEvaluation(
            case_id=case.case_id,
            mode=mode,
            outcome=(
                Outcome.PASS
                if score.recall_at_3 is None or score.recall_at_3 == 1
                else Outcome.FAIL
            ),
            score=score,
            returned_memory_ids=returned_ids,
        )

    @staticmethod
    def _inputs(case: EvalCase) -> RetrievalInput:
        if not isinstance(case.inputs, RetrievalInput):
            raise ValueError("retrieval evaluator accepts retrieval cases only")
        return case.inputs

    @staticmethod
    def _failure(
        case: EvalCase,
        mode: RetrievalMode,
        outcome: Outcome,
        reason_code: str,
    ) -> RetrievalCaseEvaluation:
        return RetrievalCaseEvaluation(
            case_id=case.case_id,
            mode=mode,
            outcome=outcome,
            reason_codes=(reason_code,),
        )


def _unique_memories(memories: Sequence[LongTermMemory]) -> tuple[LongTermMemory, ...]:
    unique: list[LongTermMemory] = []
    seen: set[str] = set()
    for memory in memories:
        if memory.memory_id not in seen:
            seen.add(memory.memory_id)
            unique.append(memory)
    return tuple(unique)


def _mean_metric(
    name: Literal["recall_at_3", "mrr_at_10"], values: Sequence[float]
) -> RetrievalMeanMetric:
    total = math.fsum(values)
    return RetrievalMeanMetric(
        name=name,
        value=(total / len(values) if values else None),
        total=total,
        denominator=len(values),
    )


def _mode_report(
    mode: RetrievalMode,
    results: Sequence[RetrievalCaseEvaluation],
) -> RetrievalModeReport:
    selected = [result for result in results if result.mode is mode]
    case_ids = [result.case_id for result in selected]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("retrieval report has duplicate case results")
    scores = [result.score for result in selected if result.score is not None]
    recalls = [score.recall_at_3 for score in scores if score.recall_at_3 is not None]
    reciprocal_ranks = [
        score.reciprocal_rank for score in scores if score.reciprocal_rank is not None
    ]
    outcomes = {outcome: 0 for outcome in Outcome}
    for result in selected:
        outcomes[result.outcome] += 1
    return RetrievalModeReport(
        mode=mode,
        attempted_cases=len(selected),
        scored_cases=len(scores),
        outcomes=outcomes,
        recall_at_3=_mean_metric("recall_at_3", recalls),
        mrr_at_10=_mean_metric("mrr_at_10", reciprocal_ranks),
        safety_failure_cases=sum(bool(result.safety_violation_codes) for result in selected),
    )


def build_retrieval_report(results: Sequence[RetrievalCaseEvaluation]) -> RetrievalReport:
    """Keep gold-fixture and formation-produced quality in distinct headline sections."""

    return RetrievalReport(
        gold_fixture=_mode_report(RetrievalMode.GOLD_FIXTURE, results),
        formation_produced=_mode_report(RetrievalMode.FORMATION_PRODUCED, results),
    )
