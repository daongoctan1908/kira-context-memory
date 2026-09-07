"""Explicit opt-in gate against the configured real memory extraction model."""

import os

import pytest

from scripts.check_live_memory_policy import evaluate_cases, options_from_environment
from tests.support.memory_policy_cases import CASES


@pytest.mark.memory_llm_integration
async def test_real_memory_model_passes_policy_corpus() -> None:
    if os.environ.get("RUN_MEMORY_POLICY_EVAL") != "1":
        pytest.skip("set RUN_MEMORY_POLICY_EVAL=1 to run the real memory-model gate")

    results = await evaluate_cases(options_from_environment(), CASES)

    failures = tuple(result for result in results if not result.passed)
    assert failures == ()
