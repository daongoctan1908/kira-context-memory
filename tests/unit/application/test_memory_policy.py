from app.application.services.memory_policy import (
    MEMORY_EXTRACTION_INSTRUCTIONS,
    MEMORY_POLICY_VERSION,
    MEMORY_TAXONOMY,
)


def test_memory_policy_v1_has_the_approved_taxonomy() -> None:
    assert MEMORY_POLICY_VERSION == "kira-memory-policy-v1"
    assert MEMORY_TAXONOMY == (
        "USER_CONTEXT",
        "ANALYSIS_PREFERENCE",
        "USER_DEFINED_METRIC",
        "USER_DEFINED_CONVENTION",
        "TEMPORARY_FOCUS",
        "EPISODIC_ANALYSIS_CONTEXT",
    )


def test_taxonomy_is_prompt_guidance_not_a_persisted_prefix() -> None:
    assert f"Policy version: {MEMORY_POLICY_VERSION}" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert all(MEMORY_EXTRACTION_INSTRUCTIONS.count(category) == 1 for category in MEMORY_TAXONOMY)
    assert "plain reusable memory fact text only" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "do not emit taxonomy metadata" in MEMORY_EXTRACTION_INSTRUCTIONS.lower()
