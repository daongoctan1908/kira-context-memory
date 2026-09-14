import json
from dataclasses import replace

import httpx
import pytest
from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT

from app.application.services.memory_policy import (
    MEMORY_EXTRACTION_INSTRUCTIONS,
    MEMORY_POLICY_VERSION,
)
from scripts.check_live_memory_policy import (
    MemoryPolicyEvalClient,
    PolicyEvalOptions,
    PolicyEvalProtocolError,
    build_extraction_messages,
    evaluate_cases,
    options_from_environment,
    parse_memory_facts,
    run,
    sanitized_report,
    score_case,
)
from tests.support.memory_policy_cases import (
    CASES,
    MEMORY_POLICY_EVAL_VERSION,
    REQUIRED_NEGATIVE_TAGS,
    validate_case_matrix,
)


def case(name: str):
    return next(item for item in CASES if item.name == name)


def test_acceptance_matrix_covers_taxonomy_and_negative_rules() -> None:
    validate_case_matrix()

    assert len(CASES) == 34
    negative_tags = {
        tag for item in CASES if not item.expectation.should_extract for tag in item.tags
    }
    assert REQUIRED_NEGATIVE_TAGS <= negative_tags


def test_build_messages_uses_exact_mem0_v3_prompt_and_policy_precedence() -> None:
    messages = build_extraction_messages(case("conversation_prompt_injection"))

    assert messages[0] == {"role": "system", "content": ADDITIVE_EXTRACTION_PROMPT}
    user_prompt = messages[1]["content"]
    assert MEMORY_EXTRACTION_INSTRUCTIONS in user_prompt
    assert "this policy takes precedence" in user_prompt
    assert user_prompt.index("## New Messages") < user_prompt.index("## Custom Instructions")
    assert user_prompt.index("## Custom Instructions") < user_prompt.index("# Output:")
    assert "## Observation Date\n2026-09-07" in user_prompt


def test_build_messages_passes_existing_memories_with_runtime_style_ids() -> None:
    correction = case("telecom_threshold_correction_existing_memory")
    prompt = build_extraction_messages(correction)[1]["content"]

    existing_section = prompt.split("## Existing Memories\n", 1)[1].split("## New Messages", 1)[0]
    assert json.loads(existing_section) == [{"id": "0", "text": correction.existing_memories[0]}]


def test_source_date_is_not_replaced_by_the_worker_observation_date() -> None:
    anchored = case("telecom_relative_focus_with_source_date")
    prompt = build_extraction_messages(anchored)[1]["content"]

    assert "13/09/2026" in prompt.split("## New Messages", 1)[1]
    assert "## Observation Date\n2026-09-20" in prompt
    assert score_case(anchored, ("Ưu tiên nghẽn 5G Hà Nội ngày 13/09/2026.",)).passed
    wrong_date = score_case(anchored, ("Ưu tiên nghẽn 5G Hà Nội ngày 20/09/2026.",))
    assert "missing_required_alternative" in wrong_date.reason_codes
    assert "forbidden_term" in wrong_date.reason_codes


def test_scoring_keeps_scope_and_exclusions_in_the_same_fact() -> None:
    scoped = case("telecom_separate_rules_keep_exclusions_attached")
    correct = (
        "Báo cáo ARPU cho trả trước không gồm M2M.",
        "Báo cáo throughput 5G loại trừ cell đang bảo dưỡng.",
    )
    assert score_case(scoped, correct).passed

    # All keywords are still present, but the exclusions now qualify the wrong rules.
    swapped = (
        "Báo cáo ARPU cho trả trước loại trừ cell đang bảo dưỡng.",
        "Báo cáo throughput 5G không gồm M2M.",
    )
    assert score_case(scoped, swapped).reason_codes == ("missing_fact_context",)


def test_scoring_rejects_changed_aggregation_operator_and_units() -> None:
    ratio = case("telecom_ratio_aggregation_exact")
    fact = (
        "Báo cáo ngày: KPI_X = SUM(success) / SUM(attempt) * 100%, chỉ tính cell có attempt >= 100."
    )
    assert score_case(ratio, (fact,)).passed
    assert not score_case(
        ratio, (fact.replace("SUM(success) / SUM(attempt)", "AVG(success / attempt)"),)
    ).passed
    assert not score_case(ratio, (fact.replace(">= 100", "> 100"),)).passed

    units = case("telecom_recurring_direction_units_timezone")
    report_rule = "Báo cáo 5G theo cell tách UL/DL, Mbps, P95, 18:00-20:00 UTC+7."
    assert score_case(units, (report_rule,)).passed
    assert not score_case(units, (report_rule.replace("Mbps", "MB/s"),)).passed


@pytest.mark.parametrize("existing", (("",), ["fact"], (None,)))
def test_cases_reject_invalid_existing_memory_context(existing) -> None:
    with pytest.raises(ValueError, match="existing memories"):
        replace(case("greeting_only"), existing_memories=existing)


@pytest.mark.parametrize("groups", (((),), (("",),)))
def test_expectations_reject_empty_per_fact_requirements(groups) -> None:
    with pytest.raises(ValueError, match="required fact groups"):
        replace(case("telecom_alarm_scope_and_window").expectation, required_fact_terms=groups)


def test_parse_memory_facts_matches_mem0_json_envelope() -> None:
    content = """```json
{"memory":[{"id":"0","text":"Người dùng thích bảng"}]}
```"""

    assert parse_memory_facts(content) == ("Người dùng thích bảng",)
    assert parse_memory_facts('{"memory": []}') == ()


@pytest.mark.parametrize(
    "content",
    (
        "",
        "not-json",
        "{}",
        '{"memory": {}}',
        '{"memory": ["not-an-object"]}',
        '{"memory": [{}]}',
        '{"memory": [{"text": "  "}]}',
        '{"memory": [{"text": "fact", "taxonomy": "USER_CONTEXT"}]}',
        '{"memory": [{"text": "fact", "attributed_to": "unknown"}]}',
    ),
)
def test_parse_memory_facts_rejects_malformed_provider_output(content: str) -> None:
    with pytest.raises(PolicyEvalProtocolError):
        parse_memory_facts(content)


def test_parse_memory_facts_preserves_native_assistant_attribution() -> None:
    content = (
        '{"memory": [{"text": "Assistant proposed threshold = 10%", "attributed_to": "assistant"}]}'
    )

    assert parse_memory_facts(content) == ("Assistant proposed threshold = 10%",)


def test_score_requires_exact_formula_and_rejects_query_contamination() -> None:
    formula_case = case("user_defined_metric_formula_exact")
    correct = (
        "Tỷ lệ giữ chân = (thuê bao cuối kỳ - thuê bao mới) / thuê bao đầu kỳ * 100%, "
        "với ngưỡng cảnh báo < 95%."
    )

    assert score_case(formula_case, (correct,)).passed is True
    assert score_case(formula_case, (correct.replace("< 95%", "dưới 95%"),)).reason_codes == (
        "missing_exact_fragment",
    )

    mixed_case = case("mixed_explicit_context_and_ordinary_query")
    contaminated = "Người dùng phụ trách miền Trung và hỏi doanh thu Hưng Yên tháng trước."
    result = score_case(mixed_case, (contaminated,))
    assert result.passed is False
    assert result.reason_codes == ("forbidden_term",)


def test_score_accepts_assistant_detail_adopted_by_explicit_user_confirmation() -> None:
    confirmed_case = case("assistant_threshold_explicitly_confirmed")

    accepted = "Assistant proposed threshold = 10%, and the user explicitly confirmed it."

    assert score_case(confirmed_case, (accepted,)).passed is True


def test_score_accepts_source_or_iso_date_but_requires_the_full_time_window() -> None:
    temporary_case = case("temporary_focus_with_explicit_window")

    source_dates = "Theo dõi tỷ lệ rớt cuộc gọi tại Hà Nội từ 01/09/2026 đến 30/09/2026."
    iso_dates = "Theo dõi tỷ lệ rớt cuộc gọi tại Hà Nội từ 2026-09-01 đến 2026-09-30."
    missing_end = "Theo dõi tỷ lệ rớt cuộc gọi tại Hà Nội từ 2026-09-01."

    assert score_case(temporary_case, (source_dates,)).passed is True
    assert score_case(temporary_case, (iso_dates,)).passed is True
    assert score_case(temporary_case, (missing_end,)).reason_codes == (
        "missing_required_alternative",
    )


def test_score_negative_case_and_taxonomy_prefix() -> None:
    negative_case = case("assistant_generated_kpi_result")
    assert score_case(negative_case, ()).passed is True
    assert score_case(negative_case, ("KPI là 1,27%",)).reason_codes == ("fact_count",)

    positive_case = case("analysis_preference_explicit")
    result = score_case(
        positive_case,
        ("ANALYSIS_PREFERENCE: Người dùng muốn so sánh theo tháng bằng bảng.",),
    )
    assert result.reason_codes == ("taxonomy_prefix",)
    bracketed = score_case(
        positive_case,
        ("[ANALYSIS PREFERENCE] Người dùng muốn so sánh theo tháng bằng bảng.",),
    )
    assert bracketed.reason_codes == ("taxonomy_prefix",)


async def test_client_calls_openai_compatible_endpoint_without_writing_memory() -> None:
    formula_case = case("user_defined_metric_formula_exact")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://memory-llm.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        payload = json.loads(request.content)
        assert payload["model"] == "qwen-memory"
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 1000
        assert payload["stream"] is False
        assert payload["response_format"] == {"type": "json_object"}
        assert MEMORY_EXTRACTION_INSTRUCTIONS in payload["messages"][1]["content"]
        content = json.dumps(
            {
                "memory": [
                    {
                        "id": "0",
                        "text": (
                            "Tỷ lệ giữ chân = (thuê bao cuối kỳ - thuê bao mới) / "
                            "thuê bao đầu kỳ * 100%, ngưỡng cảnh báo < 95%."
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    options = PolicyEvalOptions(
        "http://memory-llm.test",
        "qwen-memory",
        api_key="synthetic-key",
    )
    results = await evaluate_cases(
        options,
        (formula_case,),
        transport=httpx.MockTransport(handler),
    )

    assert results[0].passed is True
    assert results[0].fact_count == 1


async def test_client_returns_sanitized_dependency_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="private prompt and secret output")

    options = PolicyEvalOptions("http://memory-llm.test/v1", "qwen-memory")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MemoryPolicyEvalClient(client, options).evaluate(case("greeting_only"))

    assert result.outcome == "dependency_error"
    assert result.reason_codes == ("HTTPStatusError",)
    assert "private" not in json.dumps(sanitized_report((result,)))
    assert "secret" not in json.dumps(sanitized_report((result,)))


async def test_suite_stops_after_dependency_error() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503)

    results = await evaluate_cases(
        PolicyEvalOptions("http://memory-llm.test", "qwen-memory"),
        CASES,
        transport=httpx.MockTransport(handler),
    )

    assert request_count == 1
    assert len(results) == 1
    assert results[0].outcome == "dependency_error"


def test_options_use_memory_runtime_limits_and_validate_endpoint() -> None:
    options = options_from_environment(
        {
            "MEMORY_LLM_BASE_URL": "https://memory-llm.test/v1",
            "MEMORY_LLM_MODEL": "qwen-memory",
            "MEMORY_OPERATION_TIMEOUT_SECONDS": "12.5",
            "MEMORY_LLM_MAX_TOKENS": "321",
            "MEMORY_LLM_API_KEY": "secret",
        }
    )

    assert options.timeout_seconds == 12.5
    assert options.max_tokens == 321
    assert options.api_key == "secret"
    assert "secret" not in repr(options)

    with pytest.raises(ValueError):
        PolicyEvalOptions("file:///tmp/model", "qwen-memory")
    with pytest.raises(ValueError):
        PolicyEvalOptions("http://memory-llm.test", "qwen-memory", timeout_seconds=float("nan"))


def test_sanitized_report_contains_only_case_evidence() -> None:
    result = score_case(case("synthetic_secret"), ())

    report = sanitized_report((result,))

    assert report["policy_version"] == MEMORY_POLICY_VERSION
    assert report["eval_version"] == MEMORY_POLICY_EVAL_VERSION
    assert report["passed"] == 1
    assert "sk-test-DO-NOT-STORE-123" not in json.dumps(report)


async def test_cli_selects_case_and_writes_only_sanitized_report(tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"memory": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    report_path = tmp_path / "policy-report.json"
    result = await run(
        ["--case", "greeting_only", "--report", str(report_path)],
        transport=httpx.MockTransport(handler),
        environ={
            "MEMORY_LLM_BASE_URL": "http://memory-llm.test",
            "MEMORY_LLM_MODEL": "qwen-memory",
            "MEMORY_LLM_API_KEY": "must-not-leak",
        },
    )

    captured = capsys.readouterr().out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert report["total"] == 1
    assert report["passed"] == 1
    assert "must-not-leak" not in captured
    assert "Chào bạn" not in captured
    assert "must-not-leak" not in report_path.read_text(encoding="utf-8")


async def test_cli_reports_configuration_and_selection_errors_without_details(capsys) -> None:
    assert await run([], environ={}) == 2
    assert await run(["--case", "unknown"], environ={}) == 2

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        '{"outcome": "configuration_error", "error_class": "ValueError"}',
        '{"outcome": "configuration_error", "error_class": "ValueError"}',
    ]
