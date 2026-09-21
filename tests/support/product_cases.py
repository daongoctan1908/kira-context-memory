"""Synthetic-only values shared by provider mocks and product E2E smoke."""

MEMORY_MARKER = "PRODUCT_E2E_MEMORY:"
FOLLOW_UP_MARKER = "PRODUCT_E2E_FOLLOW_UP:"


def memory_fact(run_id: str) -> str:
    return f"Synthetic reporting region for run {run_id} is North."


def session_a_message(run_id: str) -> str:
    return f"{MEMORY_MARKER} {memory_fact(run_id)}"


def session_b_follow_up(run_id: str) -> str:
    return f"{FOLLOW_UP_MARKER}{run_id}: Which synthetic reporting region should be used?"


def rewritten_follow_up(run_id: str) -> str:
    return (
        f"Which reporting region should be used for run {run_id}? "
        "The user's saved preference is North."
    )
