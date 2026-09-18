"""Gold retrieval fixture ownership and public-Mem0 boundary tests."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from evaluation.compiler import compile_dataset
from evaluation.isolation import create_isolation_plan
from evaluation.models import EvalCase, RetrievalInput, Suite
from evaluation.retrieval import (
    GoldFixtureError,
    GoldRetrievalFixtureManager,
)


class RecordingFixtureClient:
    def __init__(self, *, fail_at: int | None = None, malformed: bool = False) -> None:
        self.fail_at = fail_at
        self.malformed = malformed
        self.add_calls: list[dict[str, object]] = []
        self.deleted: list[str] = []

    async def add(self, messages: object, **kwargs: object) -> object:
        self.add_calls.append({"messages": deepcopy(messages), **deepcopy(kwargs)})
        position = len(self.add_calls)
        if self.fail_at == position:
            raise RuntimeError("provider detail must not escape")
        if self.malformed:
            return {"unexpected": "raw response"}
        assert isinstance(messages, list)
        content = messages[0]["content"]
        return {
            "results": [
                {
                    "id": str(uuid5(NAMESPACE_URL, f"fixture:{position}")),
                    "memory": content,
                    "event": "ADD",
                }
            ]
        }

    async def delete(self, memory_id: str) -> object:
        self.deleted.append(memory_id)
        return {"message": "deleted"}


def _plan():
    return create_isolation_plan(
        run_id=uuid4(),
        owner_token=uuid4(),
        conversation_database_url="postgresql://eval:pw@localhost:15433/eval",
        memory_database_url="postgresql://eval:pw@localhost:15433/eval",
    )


def _retrieval_cases() -> tuple[EvalCase, ...]:
    return tuple(case for case in compile_dataset(seed=743).cases if case.suite is Suite.RETRIEVAL)


@pytest.mark.asyncio
async def test_fixture_materializes_canonical_gold_once_without_extraction_and_cleans_exact_ids():
    cases = _retrieval_cases()
    expected = {
        (memory.user_id, memory.gold_id): memory.text
        for case in cases
        for memory in case.inputs.memories  # type: ignore[union-attr]
    }
    client = RecordingFixtureClient()
    manager = GoldRetrievalFixtureManager(client, _plan())

    fixture = await manager.setup(cases)

    assert len(fixture.memories) == len(expected)
    assert set(fixture.case_ids) == {case.case_id for case in cases}
    assert all(call["infer"] is False for call in client.add_calls)
    assert all("formation_event_id" not in call["metadata"] for call in client.add_calls)
    assert {
        (memory.logical_user_id, memory.gold_id): memory.content for memory in fixture.memories
    } == expected
    assert len(fixture.gold_id_by_memory_id()) == len(expected)

    await manager.cleanup(fixture)

    assert client.deleted == [str(memory.memory_id) for memory in reversed(fixture.memories)]


@pytest.mark.asyncio
async def test_fixture_rolls_back_only_created_ids_and_hides_backend_failure_details():
    cases = _retrieval_cases()[:1]
    client = RecordingFixtureClient(fail_at=2)
    manager = GoldRetrievalFixtureManager(client, _plan())

    with pytest.raises(GoldFixtureError, match="gold_fixture_setup_failed") as captured:
        await manager.setup(cases)

    assert "provider detail" not in str(captured.value)
    assert client.deleted == [str(uuid5(NAMESPACE_URL, "fixture:1"))]


@pytest.mark.asyncio
async def test_fixture_protocol_failure_is_safe_and_another_run_has_a_distinct_user_scope():
    cases = _retrieval_cases()[:1]
    malformed = RecordingFixtureClient(malformed=True)
    with pytest.raises(GoldFixtureError, match="gold_fixture_add_protocol_error"):
        await GoldRetrievalFixtureManager(malformed, _plan()).setup(cases)

    first = await GoldRetrievalFixtureManager(RecordingFixtureClient(), _plan()).setup(cases)
    second = await GoldRetrievalFixtureManager(RecordingFixtureClient(), _plan()).setup(cases)
    assert {memory.persisted_user_id for memory in first.memories}.isdisjoint(
        memory.persisted_user_id for memory in second.memories
    )


@pytest.mark.asyncio
async def test_fixture_rejects_non_retrieval_cases_and_foreign_cleanup():
    all_cases = compile_dataset(seed=743).cases
    formation = next(case for case in all_cases if case.suite is Suite.FORMATION)
    manager = GoldRetrievalFixtureManager(RecordingFixtureClient(), _plan())
    with pytest.raises(ValueError, match="retrieval cases only"):
        await manager.setup((formation,))

    retrieval = next(case for case in all_cases if isinstance(case.inputs, RetrievalInput))
    fixture = await manager.setup((retrieval,))
    foreign = fixture.model_copy(update={"owner_token": uuid4()})
    with pytest.raises(ValueError, match="does not belong"):
        await manager.cleanup(foreign)
