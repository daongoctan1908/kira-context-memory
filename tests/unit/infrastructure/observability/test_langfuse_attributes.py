from app.infrastructure.observability.langfuse_attributes import (
    OBSERVATION_INPUT,
    OBSERVATION_MODEL,
    OBSERVATION_OUTPUT,
    masked_io_attributes,
    model_attribute,
)


def test_langfuse_io_attributes_are_masked_before_attachment() -> None:
    attributes = masked_io_attributes(
        input_value={"message": "email me at user@example.com"},
        output_value={"answer": "call +84 912 345 678"},
    )

    assert set(attributes) == {OBSERVATION_INPUT, OBSERVATION_OUTPUT}
    assert "user@example.com" not in attributes[OBSERVATION_INPUT]
    assert "+84 912 345 678" not in attributes[OBSERVATION_OUTPUT]


def test_model_attribute_rejects_unbounded_values() -> None:
    assert model_attribute("gpt-4.1-mini") == {OBSERVATION_MODEL: "gpt-4.1-mini"}
    assert model_attribute("x" * 129) == {}
