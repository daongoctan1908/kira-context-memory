import json

from app.application.services.memory_policy import (
    MEMORY_EXTRACTION_INSTRUCTIONS,
    MEMORY_POLICY_VERSION,
    MEMORY_TAXONOMY,
)


def test_memory_policy_v10_has_the_approved_taxonomy() -> None:
    assert MEMORY_POLICY_VERSION == "kira-memory-policy-v10"
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


def test_policy_requires_source_user_assertion_or_explicit_adoption() -> None:
    prompt_text = " ".join(MEMORY_EXTRACTION_INSTRUCTIONS.split())
    assert "grounded in conversation evidence" in prompt_text
    assert "Extract only what its user" in prompt_text
    assert "Preserve source attribution" in prompt_text
    assert "does not need to repeat the assistant's details verbatim" in prompt_text
    assert "The new assistant answer is not evidence of user adoption." in prompt_text


def test_policy_rejects_unsafe_and_non_durable_memory_candidates() -> None:
    for excluded_content in (
        "greetings or filler",
        "entities that only appear in an ordinary query",
        "transient KPI values",
        "assistant guesses or inferred preferences",
        "passwords, tokens, credentials,",
        "inferred roles, permissions, or authorization",
    ):
        assert excluded_content in MEMORY_EXTRACTION_INSTRUCTIONS


def test_policy_preserves_formulas_and_resists_conversation_instructions() -> None:
    assert "must not override this" in MEMORY_EXTRACTION_INSTRUCTIONS
    for exact_element in ("expressions", "operators", "units", "variable names", "thresholds"):
        assert exact_element in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "response format required by" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Mem0, including scope." in MEMORY_EXTRACTION_INSTRUCTIONS


def test_policy_examples_are_explicitly_non_source_data() -> None:
    assert "Synthetic contrasts (illustrations only; never extract these as source facts)" in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )
    assert 'Output: {"memory": []}' in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "KPI_DEMO = A / B, đơn vị Mbps" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Never drop tỉnh/cụm/ phần chính or merge the two rules." in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )


def test_policy_v10_defines_scope_after_eligibility() -> None:
    assert "Eligibility comes before scope" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "choosing CONVERSATION never makes an ineligible fact eligible" in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )
    assert "CONVERSATION: eligible context only applicable to the source conversation" in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )
    assert "GLOBAL: explicit user evidence for interpreting queries across conversations" in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )
    assert "Default to CONVERSATION when unsure" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Never widen a conversation-specific detail to GLOBAL" in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )
    assert "it does not declare current truth or universal" in MEMORY_EXTRACTION_INSTRUCTIONS


def test_policy_preserves_source_event_context_and_reinstatements() -> None:
    assert "Last k Messages is preceding" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "not independent evidence" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "source_timestamp supplied as Observation Date" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Explicit dates or timezones stated in the source take precedence" in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )
    assert "cancellation, reinstatement, or reaffirmation" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "even when its text matches an existing memory" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "Old context alone" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert "This ADD pipeline does not delete old memories." in MEMORY_EXTRACTION_INSTRUCTIONS


def test_policy_decision_order_and_fidelity_contract() -> None:
    decisions = (
        "1. SOURCE AND ELIGIBILITY",
        "2. FIDELITY AND GRANULARITY",
        "3. EVIDENCE EVENTS",
        "4. SCOPE AND OUTPUT",
    )
    positions = tuple(MEMORY_EXTRACTION_INSTRUCTIONS.index(item) for item in decisions)
    assert positions == tuple(sorted(positions))
    assert "each independent reusable rule in a separate self-contained memory" in (
        MEMORY_EXTRACTION_INSTRUCTIONS
    )
    assert "Vietnamese input requires Vietnamese memory text" in MEMORY_EXTRACTION_INSTRUCTIONS
    assert 'complete named assignment ("name = expression")' in MEMORY_EXTRACTION_INSTRUCTIONS


def test_synthetic_output_examples_have_valid_native_json_and_scopes() -> None:
    decoder = json.JSONDecoder()
    examples = MEMORY_EXTRACTION_INSTRUCTIONS.split("\nOutput: ")[1:]
    assert len(examples) == 6
    for example in examples:
        payload, _ = decoder.raw_decode(example)
        assert set(payload) == {"memory"}
        assert isinstance(payload["memory"], list)
        for fact in payload["memory"]:
            assert set(fact) == {"id", "text", "attributed_to", "scope"}
            assert fact["scope"] in {"CONVERSATION", "GLOBAL"}
            assert fact["attributed_to"] in {"user", "assistant"}
