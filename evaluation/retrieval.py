"""Run-scoped retrieval fixtures and evaluators.

Gold fixtures are written through Mem0's public ``add(..., infer=False)`` boundary.  This keeps
the native embedding/vector-store path while deliberately bypassing memory extraction.  Cleanup
is restricted to the exact memory IDs returned during setup; this module never patches runtime
tables or issues broad user-scoped deletes.
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from evaluation.isolation import IsolationPlan
from evaluation.models import EvalCase, EvalModel, Identifier, NonEmpty, RetrievalInput, SeedMemory


class GoldFixtureClient(Protocol):
    """Small public Mem0 surface required to own a disposable gold corpus."""

    async def add(self, messages: object, **kwargs: Any) -> object: ...

    async def delete(self, memory_id: str) -> object: ...


class GoldFixtureError(RuntimeError):
    """Content-free fixture failure safe to persist as a dependency diagnostic."""


class GoldFixtureMemory(EvalModel):
    gold_id: Identifier
    logical_user_id: Identifier
    persisted_user_id: Identifier
    memory_id: UUID
    content: NonEmpty


class GoldRetrievalFixture(EvalModel):
    """Exact mapping between canonical gold IDs and run-local backend IDs."""

    schema_version: Literal[1] = 1
    run_id: UUID
    owner_token: UUID
    memory_schema: str = Field(pattern=r"^eval_[a-f0-9]{12}$")
    memory_collection: str = Field(pattern=r"^mem_[a-f0-9]{12}$")
    case_ids: tuple[Identifier, ...]
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
        persisted_by_logical: dict[str, str] = {}
        for memory in self.memories:
            existing = persisted_by_logical.setdefault(
                memory.logical_user_id,
                memory.persisted_user_id,
            )
            if existing != memory.persisted_user_id:
                raise ValueError("one logical user cannot span gold fixture user scopes")
        return self

    def persisted_user_id(self, logical_user_id: str) -> str:
        users = {
            memory.persisted_user_id
            for memory in self.memories
            if memory.logical_user_id == logical_user_id
        }
        if len(users) != 1:
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

    def __init__(self, client: GoldFixtureClient, plan: IsolationPlan) -> None:
        self._client = client
        self._plan = plan

    async def setup(self, cases: Sequence[EvalCase]) -> GoldRetrievalFixture:
        case_ids = tuple(case.case_id for case in cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("gold fixture retrieval cases must be unique")
        seeds = _gold_memories(cases)
        created: list[GoldFixtureMemory] = []
        try:
            for seed in seeds:
                persisted_user_id = _persisted_fixture_user(self._plan, seed.user_id)
                response = await self._client.add(
                    [{"role": "user", "content": seed.text}],
                    user_id=persisted_user_id,
                    metadata={
                        "eval_fixture": "gold_retrieval",
                        "eval_run_id": str(self._plan.run_id),
                        "gold_memory_id": seed.gold_id,
                        "logical_user_id": seed.user_id,
                    },
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
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
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
            memories=tuple(created),
        )

    async def cleanup(self, fixture: GoldRetrievalFixture) -> None:
        self._validate_owner(fixture)
        failed = await self._delete_exact(fixture.memories)
        if failed:
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
