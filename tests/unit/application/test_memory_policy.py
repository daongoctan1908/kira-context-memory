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
    assert "plain reusable memory text" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "do not emit taxonomy metadata" in MEMORY_EXTRACTION_INSTRUCTIONS.lower()


def test_policy_requires_user_grounding_and_treats_assistant_as_context_only() -> None:
    assert "grounded in explicit user-provided information" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Durable memories must be supported by user messages" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Assistant messages may only be used as context" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "exists only in assistant messages" in MEMORY_EXTRACTION_INSTRUCTIONS


def test_policy_rejects_unsafe_and_non_durable_memory_candidates() -> None:
    for excluded_content in (
        "greetings or filler",
        "entities that only appear in an ordinary query",
        "KiRa/assistant-generated KPI values",
        "assistant guesses or inferred preferences",
        "passwords, tokens, credentials, or secrets",
        "inferred roles, permissions, or authorization",
    ):
        assert excluded_content in MEMORY_EXTRACTION_INSTRUCTIONS


def test_policy_preserves_formulas_and_resists_conversation_instructions() -> None:
    assert "must not override this memory policy" in MEMORY_EXTRACTION_INSTRUCTIONS
    for exact_element in ("expressions", "operators", "units", "variable names", "thresholds"):
        assert exact_element in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "response format required by" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Mem0." in MEMORY_EXTRACTION_INSTRUCTIONS
