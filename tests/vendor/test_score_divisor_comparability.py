"""Characterization: raw-score comparability across branch filters (T0.2).

Documents that the combined score divisor depends on per-branch candidate
population (BM25/entity signals), so raw-score merging of two independently
filtered searches is biased toward the branch without those signals.
"""

from mem0.utils.scoring import score_and_rank


def _candidates(*scores: float) -> list[dict]:
    return [
        {"id": f"mem-{index}", "score": score, "payload": {}} for index, score in enumerate(scores)
    ]


def test_same_semantic_scores_scored_divisor_agnostic_when_no_auxiliary_signals():
    scored = score_and_rank(
        semantic_results=_candidates(0.8, 0.6),
        bm25_scores={},
        entity_boosts={},
        threshold=0.1,
        top_k=10,
    )

    assert [item["score"] for item in scored] == [0.8, 0.6]


def test_branch_with_bm25_hit_divides_by_2_but_branch_without_keeps_divisor_1():
    same_semantic = 0.8

    branch_a = score_and_rank(
        semantic_results=_candidates(same_semantic),
        bm25_scores={"mem-0": 0.0},
        entity_boosts={},
        threshold=0.1,
        top_k=10,
    )
    branch_b = score_and_rank(
        semantic_results=_candidates(same_semantic),
        bm25_scores={},
        entity_boosts={},
        threshold=0.1,
        top_k=10,
    )

    # Same semantic score, same raw_combined — but divisor differs by branch
    # BM25 candidate population (0.0-scored BM25 hit still activates has_bm25).
    assert branch_a[0]["score"] == same_semantic / 2.0
    assert branch_b[0]["score"] == same_semantic


def test_entity_boost_for_out_of_candidates_id_does_not_inflate_divisor():
    # Boost keyed to an id that is NOT in the candidate set: fix C keeps the
    # divisor at 1.0 because has_entity intersects boost keys with candidate ids.
    scored = score_and_rank(
        semantic_results=_candidates(0.8),
        bm25_scores={},
        entity_boosts={"mem-out-of-candidates": 0.4},
        threshold=0.1,
        top_k=10,
    )

    assert scored[0]["score"] == 0.8


def test_entity_boost_for_candidate_id_raises_divisor_and_score():
    scored = score_and_rank(
        semantic_results=_candidates(0.8),
        bm25_scores={},
        entity_boosts={"mem-0": 0.4},
        threshold=0.1,
        top_k=10,
    )

    assert scored[0]["score"] == (0.8 + 0.4) / 1.5
