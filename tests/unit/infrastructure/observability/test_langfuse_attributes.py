import json

from app.infrastructure.observability.langfuse_attributes import (
    OBSERVATION_INPUT,
    OBSERVATION_MODEL,
    OBSERVATION_OUTPUT,
    OBSERVATION_USAGE,
    masked_io_attributes,
    model_attribute,
    searchable_trace_metadata,
    usage_attributes,
)


class BrokenMapping(dict):
    def items(self):
        raise RuntimeError("masking failed")


def test_langfuse_io_attributes_are_masked_before_attachment() -> None:
    attributes = masked_io_attributes(
        input_value={"message": "email me at user@example.com"},
        output_value={"answer": "call +84 912 345 678"},
    )

    assert set(attributes) == {
        OBSERVATION_INPUT,
        OBSERVATION_OUTPUT,
        "kira.observation.input.truncated",
        "kira.observation.input.original_bytes",
        "kira.observation.output.truncated",
        "kira.observation.output.original_bytes",
    }
    assert "user@example.com" not in attributes[OBSERVATION_INPUT]
    assert "+84 912 345 678" not in attributes[OBSERVATION_OUTPUT]


def test_model_attribute_rejects_unbounded_values() -> None:
    assert model_attribute("gpt-4.1-mini") == {OBSERVATION_MODEL: "gpt-4.1-mini"}
    assert model_attribute("x" * 129) == {}


def test_usage_attributes_accept_only_provider_reported_nonnegative_counts() -> None:
    attributes = usage_attributes({"input": 12, "output": 3, "total": 15})

    assert attributes[OBSERVATION_USAGE] == '{"input":12,"output":3,"total":15}'
    assert attributes["gen_ai.usage.input_tokens"] == 12
    assert attributes["gen_ai.usage.output_tokens"] == 3
    assert usage_attributes(None) == {}
    assert usage_attributes({"input": "12", "output": -1}) == {}


def test_io_attributes_report_safe_truncation_metadata() -> None:
    attributes = masked_io_attributes(input_value={"message": "x" * 20_000})

    assert len(attributes[OBSERVATION_INPUT]) <= 4096
    assert json.loads(attributes[OBSERVATION_INPUT]).endswith("[TRUNCATED]")
    assert attributes["kira.observation.input.truncated"] is True
    assert attributes["kira.observation.input.original_bytes"] > 4096


def test_masking_failure_omits_content_without_exposing_exception() -> None:
    attributes = masked_io_attributes(
        input_value=BrokenMapping(secret="private-input"),
        output_value=BrokenMapping(secret="private-output"),
    )

    assert OBSERVATION_INPUT not in attributes
    assert OBSERVATION_OUTPUT not in attributes
    assert attributes["kira.observation.input.content_omitted"] == "masking_error"
    assert attributes["kira.observation.output.content_omitted"] == "masking_error"
    assert "private" not in str(attributes)


def test_invalid_unicode_io_is_omitted_without_raising() -> None:
    attributes = masked_io_attributes(input_value="\ud800", output_value={"message": "\udfff"})

    assert attributes == {
        "kira.observation.input.content_omitted": "masking_error",
        "kira.observation.output.content_omitted": "masking_error",
    }


def test_only_reviewed_identifiers_become_searchable_trace_metadata() -> None:
    assert searchable_trace_metadata("correlation_id", "abc-123") == (
        "langfuse.trace.metadata.correlation_id",
        "abc-123",
    )
    assert searchable_trace_metadata("content", "private") is None
    assert searchable_trace_metadata("event_id", "x" * 129) is None
