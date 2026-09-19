"""Two-mode retrieval evaluation without formation or chat side effects."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.errors.memory import (
    LongTermMemoryConnectionError,
    LongTermMemoryProtocolError,
)
from app.domain.models.memory import LongTermMemory
from evaluation.artifacts import (
    ArtifactRunIdentity,
    FormedCorpusArtifact,
    FormedCorpusCaseArtifact,
    FormedMemoryArtifact,
)
from evaluation.isolation import create_isolation_plan
from evaluation.models import (
    BenchmarkVariant,
    CaseEligibility,
    EvalCase,
    GitSource,
    GoldSpecification,
    Outcome,
    Profile,
    RetrievalInput,
    RunProvenance,
    SeedMemory,
    Suite,
)
from evaluation.retrieval import (
    FormationRetrievalFixture,
    FormationRetrievalFixtureManager,
    FormedFixtureMemory,
    GoldFixtureMemory,
    GoldRetrievalFixture,
    RetrievalEvaluator,
    RetrievalFixtureUserScope,
    build_retrieval_report,
)

_NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
_LOGICAL_USER = "conv01:user"
_PERSISTED_USER = "eval:aaaaaaaaaaaa:gold:1111111111111111"


class SearchOnlyMemory:
    def __init__(
        self,
        results: tuple[LongTermMemory, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.results = results
        self.error = error
        self.calls: list[tuple[str, str, int, float]] = []

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        self.calls.append((user_id, query, top_k, threshold))
        if self.error is not None:
            raise self.error
        return self.results

    async def process_memory(self, source: object) -> object:
        raise AssertionError("retrieval evaluation must never enqueue formation")


class RecordingFixtureClient:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []
        self.deleted: list[str] = []

    async def add(self, messages: object, **kwargs: object) -> object:
        self.add_calls.append({"messages": messages, **kwargs})
        memory_id = uuid4()
        assert isinstance(messages, list)
        return {
            "results": [
                {
                    "id": str(memory_id),
                    "memory": messages[0]["content"],
                    "event": "ADD",
                }
            ]
        }

    async def delete(self, memory_id: str) -> object:
        self.deleted.append(memory_id)
        return {"message": "deleted"}


def _case(*, relevant: tuple[str, ...] = ("gold-1",)) -> EvalCase:
    memories = tuple(
        SeedMemory(
            gold_id=f"gold-{index}",
            user_id=_LOGICAL_USER,
            text=f"canonical memory {index}",
        )
        for index in range(1, 11)
    )
    return EvalCase(
        case_id="conv01:retrieval:q1",
        family_id="conv01:scenario:lookup",
        evaluation_scope="full_corpus",
        provenance="synthetic",
        source_row_ids=("conv01:qa:q1",),
        inputs=RetrievalInput(
            user_id=_LOGICAL_USER,
            current_query="Tìm thông tin đã lưu",
            memories=memories,
        ),
        gold=GoldSpecification(
            relevant_memory_ids=relevant,
            semantic_expectation="Retrieve the required memory",
        ),
    )


def _gold_fixture(case: EvalCase) -> GoldRetrievalFixture:
    assert isinstance(case.inputs, RetrievalInput)
    return GoldRetrievalFixture(
        run_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        owner_token=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        memory_schema="eval_aaaaaaaaaaaa",
        memory_collection="mem_aaaaaaaaaaaa",
        case_ids=(case.case_id,),
        user_scopes=(
            RetrievalFixtureUserScope(
                logical_user_id=_LOGICAL_USER,
                persisted_user_id=_PERSISTED_USER,
            ),
        ),
        memories=tuple(
            GoldFixtureMemory(
                gold_id=memory.gold_id,
                logical_user_id=_LOGICAL_USER,
                persisted_user_id=_PERSISTED_USER,
                memory_id=UUID(f"00000000-0000-4000-8000-{index:012d}"),
                content=memory.text,
            )
            for index, memory in enumerate(case.inputs.memories, 1)
        ),
    )


def _result(fixture: GoldRetrievalFixture, index: int, *, owner: str = _PERSISTED_USER):
    memory = fixture.memories[index]
    return LongTermMemory(
        memory_id=str(memory.memory_id),
        content=memory.content,
        score=1 - index / 100,
        metadata={"user_id": owner},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rank", [1, 2, 3, 10])
async def test_gold_fixture_scores_hits_at_locked_ranks(rank: int):
    case = _case()
    fixture = _gold_fixture(case)
    target = _result(fixture, 0)
    noise = [_result(fixture, index) for index in range(1, 10)]
    ranked = tuple([*noise[: rank - 1], target, *noise[rank - 1 :]])
    memory = SearchOnlyMemory(ranked)
    evaluator = RetrievalEvaluator(
        memory,
        profile=Profile.MOCK,
        backend="mock",
        threshold=0.3,
    )

    result = await evaluator.evaluate_gold_fixture(case, fixture)

    assert result.score is not None
    assert result.score.recall_at_3 == (1 if rank <= 3 else 0)
    assert result.score.reciprocal_rank == pytest.approx(1 / rank)
    assert result.outcome is (Outcome.PASS if rank <= 3 else Outcome.FAIL)
    assert memory.calls == [(_PERSISTED_USER, case.inputs.current_query, 10, 0.3)]


@pytest.mark.asyncio
async def test_duplicate_backend_ids_do_not_consume_rank_or_add_credit():
    case = _case(relevant=("gold-1", "gold-2"))
    fixture = _gold_fixture(case)
    noise = _result(fixture, 2)
    first = _result(fixture, 0)
    second = _result(fixture, 1)
    evaluator = RetrievalEvaluator(
        SearchOnlyMemory((noise, first, first, second)),
        profile=Profile.MOCK,
        backend="mock",
    )

    result = await evaluator.evaluate_gold_fixture(case, fixture)

    assert result.returned_memory_ids == (
        UUID(noise.memory_id),
        UUID(first.memory_id),
        UUID(first.memory_id),
        UUID(second.memory_id),
    )
    assert result.score is not None
    assert result.score.recall_at_3 == 1
    assert result.score.reciprocal_rank == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_wrong_user_and_foreign_run_results_are_hard_failures():
    case = _case()
    fixture = _gold_fixture(case)
    wrong_user = RetrievalEvaluator(
        SearchOnlyMemory((_result(fixture, 0, owner="another-user"),)),
        profile=Profile.MOCK,
        backend="mock",
    )
    wrong_user_result = await wrong_user.evaluate_gold_fixture(case, fixture)
    assert wrong_user_result.outcome is Outcome.FAIL
    assert wrong_user_result.score is None
    assert wrong_user_result.safety_violation_codes == ("retrieval_cross_user_result",)

    foreign_memory = LongTermMemory(
        memory_id=str(uuid4()),
        content="memory from another run",
        score=0.9,
        metadata={"user_id": _PERSISTED_USER},
    )
    foreign = RetrievalEvaluator(
        SearchOnlyMemory((foreign_memory,)),
        profile=Profile.MOCK,
        backend="mock",
    )
    foreign_result = await foreign.evaluate_gold_fixture(case, fixture)
    assert foreign_result.outcome is Outcome.FAIL
    assert foreign_result.safety_violation_codes == ("retrieval_foreign_corpus_result",)


@pytest.mark.asyncio
async def test_no_hit_has_no_metric_denominator_and_dependency_errors_are_not_misses():
    case = _case(relevant=())
    fixture = _gold_fixture(case)
    evaluator = RetrievalEvaluator(
        SearchOnlyMemory(),
        profile=Profile.MOCK,
        backend="mock",
    )
    no_hit = await evaluator.evaluate_gold_fixture(case, fixture)
    assert no_hit.outcome is Outcome.PASS
    assert no_hit.score is not None
    assert no_hit.score.recall_at_3 is None
    assert no_hit.score.reciprocal_rank is None

    unavailable = await evaluator.evaluate_gold_fixture(case, None)
    missing_formed = await evaluator.evaluate_formation_produced(case, None)
    assert unavailable.outcome is Outcome.DEPENDENCY_ERROR and unavailable.score is None
    assert unavailable.reason_codes == ("gold_fixture_unavailable",)
    assert missing_formed.outcome is Outcome.DEPENDENCY_ERROR and missing_formed.score is None
    assert missing_formed.reason_codes == ("formed_corpus_unavailable",)


@pytest.mark.asyncio
async def test_empty_but_available_formed_corpus_is_a_semantic_miss_not_dependency_error():
    case = _case()
    fixture = FormationRetrievalFixture(
        run_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        owner_token=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        memory_schema="eval_aaaaaaaaaaaa",
        memory_collection="mem_aaaaaaaaaaaa",
        case_ids=(case.case_id,),
        user_scopes=(
            RetrievalFixtureUserScope(
                logical_user_id=_LOGICAL_USER,
                persisted_user_id="eval:aaaaaaaaaaaa:formed:1111111111111111",
            ),
        ),
        memories=(),
    )
    evaluator = RetrievalEvaluator(
        SearchOnlyMemory(),
        profile=Profile.MOCK,
        backend="mock",
    )

    result = await evaluator.evaluate_formation_produced(case, fixture)

    assert result.outcome is Outcome.FAIL
    assert result.score is not None
    assert result.score.recall_at_3 == 0
    assert result.score.reciprocal_rank == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "outcome", "reason"),
    [
        (LongTermMemoryConnectionError(), Outcome.DEPENDENCY_ERROR, "retrieval_unavailable"),
        (LongTermMemoryProtocolError(), Outcome.PROTOCOL_ERROR, "retrieval_protocol_error"),
    ],
)
async def test_provider_and_protocol_errors_are_not_semantic_misses(error, outcome, reason):
    case = _case()
    evaluator = RetrievalEvaluator(
        SearchOnlyMemory(error=error),
        profile=Profile.MOCK,
        backend="mock",
    )
    result = await evaluator.evaluate_gold_fixture(case, _gold_fixture(case))
    assert result.outcome is outcome
    assert result.score is None
    assert result.reason_codes == (reason,)


@pytest.mark.asyncio
async def test_invalid_backend_id_is_protocol_error_and_blocked_case_never_searches():
    case = _case()
    fixture = _gold_fixture(case)
    malformed = LongTermMemory(
        memory_id="not-a-uuid",
        content="invalid backend row",
        score=0.9,
        metadata={"user_id": _PERSISTED_USER},
    )
    invalid_evaluator = RetrievalEvaluator(
        SearchOnlyMemory((malformed,)),
        profile=Profile.MOCK,
        backend="mock",
    )
    invalid = await invalid_evaluator.evaluate_gold_fixture(case, fixture)
    assert invalid.outcome is Outcome.PROTOCOL_ERROR
    assert invalid.reason_codes == ("retrieval_memory_id_invalid",)

    blocked_memory = SearchOnlyMemory()
    blocked_evaluator = RetrievalEvaluator(
        blocked_memory,
        profile=Profile.MOCK,
        backend="mock",
    )
    blocked_case = case.model_copy(
        update={
            "eligibility": CaseEligibility(
                status="blocked",
                blocked_reasons=("pending_kira_answer",),
            )
        }
    )
    blocked = await blocked_evaluator.evaluate_gold_fixture(blocked_case, None)
    assert blocked.outcome is Outcome.NOT_RUN
    assert blocked.reason_codes == ("retrieval_case_blocked",)
    assert blocked_memory.calls == []


def _identity(run_id: UUID, formation_case_id: str) -> ArtifactRunIdentity:
    source = GitSource(sha="1" * 40, dirty=False)
    return ArtifactRunIdentity(
        run_id=run_id,
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
        seed=743,
        suites=(Suite.FORMATION, Suite.RETRIEVAL),
        selected_case_ids=(formation_case_id, "conv01:retrieval:q1"),
    )


def _formed_corpus(run_id: UUID) -> FormedCorpusArtifact:
    formation_case_id = "conv01:formation:gold-1"
    source_memory_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    event_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    return FormedCorpusArtifact(
        created_at=_NOW,
        identity=_identity(run_id, formation_case_id),
        cases=(
            FormedCorpusCaseArtifact(
                case_id=formation_case_id,
                family_id="conv01:memory-family:preference",
                logical_user_id=_LOGICAL_USER,
                persisted_user_id="eval:source:user",
                formation_event_id=event_id,
                source_gold_ids=("gold-1",),
                memory_ids=(source_memory_id,),
            ),
        ),
        memories=(
            FormedMemoryArtifact(
                case_id=formation_case_id,
                family_id="conv01:memory-family:preference",
                source_gold_ids=("gold-1",),
                logical_user_id=_LOGICAL_USER,
                persisted_user_id="eval:source:user",
                memory_id=source_memory_id,
                content="formation output, not canonical gold text",
                formation_event_id=event_id,
                conversation_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                turn_id="turn-1",
                boundary_message_id=1,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_formation_mode_materializes_exact_formed_content_and_reports_separately():
    case = _case()
    plan = create_isolation_plan(
        run_id=uuid4(),
        owner_token=uuid4(),
        conversation_database_url="postgresql://eval:pw@localhost:15433/eval",
        memory_database_url="postgresql://eval:pw@localhost:15433/eval",
    )
    client = RecordingFixtureClient()
    manager = FormationRetrievalFixtureManager(client, plan)
    fixture = await manager.setup(_formed_corpus(plan.run_id), (case,))
    assert client.add_calls[0]["messages"] == [
        {"role": "user", "content": "formation output, not canonical gold text"}
    ]
    assert client.add_calls[0]["infer"] is False

    memory = fixture.memories[0]
    found = LongTermMemory(
        memory_id=str(memory.memory_id),
        content=memory.content,
        score=0.9,
        metadata={"user_id": memory.persisted_user_id},
    )
    evaluator = RetrievalEvaluator(
        SearchOnlyMemory((found,)),
        profile=Profile.MOCK,
        backend="mock",
    )
    formed_result = await evaluator.evaluate_formation_produced(case, fixture)
    gold_result = await evaluator.evaluate_gold_fixture(case, None)
    report = build_retrieval_report((formed_result, gold_result))

    assert formed_result.outcome is Outcome.PASS
    assert formed_result.score is not None and formed_result.score.recall_at_3 == 1
    assert report.formation_produced.recall_at_3.value == 1
    assert report.formation_produced.mrr_at_10.value == 1
    assert report.gold_fixture.recall_at_3.value is None
    assert report.gold_fixture.outcomes[Outcome.DEPENDENCY_ERROR] == 1
    assert "precision" not in report.model_dump(mode="json")
    assert "overall" not in report.model_dump_json()

    await manager.cleanup(fixture)
    assert client.deleted == [str(memory.memory_id)]


def test_official_profile_rejects_mock_and_depth_is_locked():
    memory = SearchOnlyMemory()
    with pytest.raises(ValueError, match="native adapter"):
        RetrievalEvaluator(memory, profile=Profile.INTERNAL_TEST, backend="mock")
    with pytest.raises(ValueError, match="depth is locked"):
        RetrievalEvaluator(memory, profile=Profile.MOCK, backend="mock", top_k=3)


def test_formation_fixture_keeps_multi_gold_mapping_per_rank():
    fixture = FormationRetrievalFixture(
        run_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        owner_token=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        memory_schema="eval_aaaaaaaaaaaa",
        memory_collection="mem_aaaaaaaaaaaa",
        case_ids=("conv01:retrieval:q1",),
        user_scopes=(
            RetrievalFixtureUserScope(
                logical_user_id=_LOGICAL_USER,
                persisted_user_id="eval:aaaaaaaaaaaa:formed:1111111111111111",
            ),
        ),
        memories=(
            FormedFixtureMemory(
                source_gold_ids=("gold-1", "gold-2"),
                logical_user_id=_LOGICAL_USER,
                persisted_user_id="eval:aaaaaaaaaaaa:formed:1111111111111111",
                source_memory_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                memory_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                content="one formed row representing two expected facts",
            ),
        ),
    )
    assert fixture.gold_ids_by_memory_id() == {
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd": ("gold-1", "gold-2")
    }
