"""Lock the native retrieval score semantics and candidate comparison boundary."""

from uuid import UUID

import pytest
from mem0.utils.scoring import score_and_rank
from pydantic import ValidationError

from app.infrastructure.memory.mem0_adapter import Mem0Adapter
from evaluation.retrieval_contract import (
    RetrievalConfigDeclaration,
    RetrievalConfigRegistry,
    RetrievalRuntimeConfig,
    RetrievalScoreContract,
    declare_retrieval_config,
    retrieval_config_sha256,
)


def _runtime_config(
    *,
    top_k: int = 10,
    threshold: float = 0.1,
    model: str = "internal/embedding-v1",
    dimensions: int = 1536,
) -> RetrievalRuntimeConfig:
    return RetrievalRuntimeConfig(
        top_k=top_k,
        threshold=threshold,
        embedding_model=model,
        embedding_dimensions=dimensions,
    )


def test_score_contract_names_semantic_and_returned_scores_separately():
    contract = RetrievalScoreContract()

    assert contract.semantic_score == "pgvector_cosine_similarity"
    assert contract.threshold_applies_to == "semantic_score_before_hybrid"
    assert contract.returned_score == "normalized_hybrid_score"
    assert contract.score_direction == "higher_is_better"
    assert contract.truncation == "rank_descending_then_top_k"
    assert contract.candidate_overfetch == "max_top_k_times_4_or_60"
    assert contract.retrieval_depth == 10
    assert contract.recall_cutoff == 3


def test_native_scoring_filters_semantic_before_hybrid_and_returns_hybrid_score():
    ranked = score_and_rank(
        semantic_results=[
            {"id": "below", "score": 0.49, "payload": {}},
            {"id": "kept", "score": 0.8, "payload": {}},
        ],
        bm25_scores={"below": 1.0, "kept": 1.0},
        entity_boosts={},
        threshold=0.5,
        top_k=10,
        explain=True,
    )

    assert [row["id"] for row in ranked] == ["kept"]
    assert ranked[0]["score"] == pytest.approx(0.9)
    assert ranked[0]["score"] != ranked[0]["score_details"]["semantic_score"]
    assert ranked[0]["score_details"]["threshold"] == 0.5


def test_native_scoring_sorts_descending_then_truncates_top_k():
    ranked = score_and_rank(
        semantic_results=[
            {"id": "low", "score": 0.3, "payload": {}},
            {"id": "high", "score": 0.9, "payload": {}},
            {"id": "middle", "score": 0.6, "payload": {}},
        ],
        bm25_scores={},
        entity_boosts={},
        threshold=0,
        top_k=2,
    )

    assert [(row["id"], row["score"]) for row in ranked] == [
        ("high", 0.9),
        ("middle", 0.6),
    ]


class _RecordingSearchClient:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def search(self, query: str, **kwargs: object) -> object:
        self.kwargs = {"query": query, **kwargs}
        return {
            "results": [
                {
                    "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "memory": "Gold memory",
                    "score": 0.73,
                    "user_id": "user-1",
                    "metadata": {"user_id": "user-1"},
                }
            ]
        }


@pytest.mark.asyncio
async def test_adapter_forwards_exact_user_filter_and_preserves_native_score():
    client = _RecordingSearchClient()
    adapter = Mem0Adapter(
        client,  # type: ignore[arg-type]
        search_timeout_seconds=1,
        operation_timeout_seconds=1,
    )

    rows = await adapter.search("user-1", "query", top_k=7, threshold=0.25)

    assert client.kwargs == {
        "query": "query",
        "top_k": 7,
        "threshold": 0.25,
        "filters": {"user_id": "user-1"},
        "rerank": False,
    }
    assert rows[0].memory_id == str(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert rows[0].score == 0.73


def test_config_hash_is_stable_and_registry_allows_only_one_comparable_candidate():
    control_config = _runtime_config()
    candidate_config = _runtime_config(threshold=0.3)
    control = declare_retrieval_config(
        config_id="retrieval-control",
        role="control",
        config=control_config,
    )
    candidate = declare_retrieval_config(
        config_id="retrieval-threshold-03",
        role="candidate",
        config=candidate_config,
        declared_differences=("Raise semantic threshold from 0.1 to 0.3.",),
    )
    registry = RetrievalConfigRegistry(configs=(control, candidate))

    assert control.config_sha256 == retrieval_config_sha256(control_config)
    assert control.config_sha256 != candidate.config_sha256
    assert registry.configs == (control, candidate)

    second_candidate = declare_retrieval_config(
        config_id="retrieval-top-k-5",
        role="candidate",
        config=_runtime_config(top_k=5),
        declared_differences=("Limit runtime context to five memories.",),
    )
    with pytest.raises(ValidationError, match="at most 2 items"):
        RetrievalConfigRegistry(configs=(control, candidate, second_candidate))


def test_registry_rejects_embedding_space_changes_and_bad_declarations():
    control = declare_retrieval_config(
        config_id="retrieval-control",
        role="control",
        config=_runtime_config(),
    )
    other_model = declare_retrieval_config(
        config_id="different-embedding",
        role="candidate",
        config=_runtime_config(model="internal/embedding-v2"),
        declared_differences=("Change embedding model.",),
    )
    with pytest.raises(ValidationError, match="cannot be compared"):
        RetrievalConfigRegistry(configs=(control, other_model))

    with pytest.raises(ValidationError, match="hash does not match"):
        RetrievalConfigDeclaration(
            config_id="bad-hash",
            role="candidate",
            config=_runtime_config(threshold=0.3),
            config_sha256="f" * 64,
            declared_differences=("Change threshold.",),
        )

    with pytest.raises(ValidationError, match="must declare"):
        declare_retrieval_config(
            config_id="undeclared",
            role="candidate",
            config=_runtime_config(threshold=0.3),
        )


def test_registry_rejects_duplicate_or_missing_control():
    control_config = _runtime_config()
    control = declare_retrieval_config(
        config_id="retrieval-control",
        role="control",
        config=control_config,
    )
    duplicate = control.model_copy(update={"config_id": "duplicate"})
    with pytest.raises(ValidationError, match="must be distinct"):
        RetrievalConfigRegistry(configs=(control, duplicate))

    candidate = declare_retrieval_config(
        config_id="candidate-only",
        role="candidate",
        config=_runtime_config(threshold=0.2),
        declared_differences=("Change threshold.",),
    )
    with pytest.raises(ValidationError, match="exactly one control"):
        RetrievalConfigRegistry(configs=(candidate,))
