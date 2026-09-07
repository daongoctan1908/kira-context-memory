"""Versioned domain guidance for long-term-memory extraction."""

MEMORY_POLICY_VERSION = "kira-memory-policy-v1"

MEMORY_TAXONOMY: tuple[str, ...] = (
    "USER_CONTEXT",
    "ANALYSIS_PREFERENCE",
    "USER_DEFINED_METRIC",
    "USER_DEFINED_CONVENTION",
    "TEMPORARY_FOCUS",
    "EPISODIC_ANALYSIS_CONTEXT",
)

MEMORY_EXTRACTION_INSTRUCTIONS = f"""Policy version: {MEMORY_POLICY_VERSION}

Use this taxonomy only as internal extraction guidance:
- USER_CONTEXT: explicit user responsibility, business scope, region, domain, or service context.
- ANALYSIS_PREFERENCE: explicit preferences for analysis, comparison, breakdown, or presentation.
- USER_DEFINED_METRIC: user-defined KPI or metric names and their defining expressions.
- USER_DEFINED_CONVENTION: explicit reusable conventions supplied by the user.
- TEMPORARY_FOCUS: an explicitly stated temporary focus with its stated time scope.
- EPISODIC_ANALYSIS_CONTEXT: reusable context from a user-confirmed analytical episode.

Return plain reusable memory fact text only. Do not prefix the fact with a taxonomy name.
Do not emit taxonomy metadata. The taxonomy is a policy vocabulary, not a persisted memory schema.
"""
