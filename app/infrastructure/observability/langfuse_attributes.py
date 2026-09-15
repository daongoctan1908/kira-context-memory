"""Reviewed Langfuse OTel attribute names and masked value builders."""

from app.infrastructure.observability.redaction import masked_json, safe_log_value

OBSERVATION_TYPE = "langfuse.observation.type"
OBSERVATION_INPUT = "langfuse.observation.input"
OBSERVATION_OUTPUT = "langfuse.observation.output"
OBSERVATION_MODEL = "langfuse.observation.model.name"
TRACE_USER_ID = "langfuse.trace.user_id"
TRACE_SESSION_ID = "langfuse.trace.session_id"


def masked_io_attributes(
    *, input_value: object = None, output_value: object = None
) -> dict[str, str]:
    """Build bounded masked observation attributes; generic instrumentation never calls this."""
    attributes: dict[str, str] = {}
    if input_value is not None:
        attributes[OBSERVATION_INPUT] = masked_json(input_value)
    if output_value is not None:
        attributes[OBSERVATION_OUTPUT] = masked_json(output_value)
    return attributes


def model_attribute(model: object) -> dict[str, str]:
    """Return a bounded model attribute or no attribute for unsafe input."""
    safe_model = safe_log_value(model)
    return {OBSERVATION_MODEL: safe_model} if isinstance(safe_model, str) else {}
