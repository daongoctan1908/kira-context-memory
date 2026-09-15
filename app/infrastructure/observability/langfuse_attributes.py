"""Reviewed Langfuse OTel attribute names and masked value builders."""

import json
from collections.abc import Mapping

from app.infrastructure.observability.redaction import masked_json_snapshot, safe_log_value

OBSERVATION_TYPE = "langfuse.observation.type"
OBSERVATION_INPUT = "langfuse.observation.input"
OBSERVATION_OUTPUT = "langfuse.observation.output"
OBSERVATION_MODEL = "langfuse.observation.model.name"
OBSERVATION_USAGE = "langfuse.observation.usage_details"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
TRACE_USER_ID = "langfuse.trace.user_id"
TRACE_SESSION_ID = "langfuse.trace.session_id"
TRACE_METADATA_PREFIX = "langfuse.trace.metadata."
SEARCHABLE_TRACE_IDENTIFIERS = frozenset(
    {"correlation_id", "turn_id", "event_id", "origin_trace_id"}
)


def searchable_trace_metadata(key: object, value: object) -> tuple[str, str] | None:
    """Map one reviewed application identifier to filterable Langfuse metadata."""
    safe_key = safe_log_value(key)
    safe_value = safe_log_value(value)
    if (
        not isinstance(safe_key, str)
        or safe_key not in SEARCHABLE_TRACE_IDENTIFIERS
        or not isinstance(safe_value, str)
    ):
        return None
    return f"{TRACE_METADATA_PREFIX}{safe_key}", safe_value


def masked_io_attributes(
    *, input_value: object = None, output_value: object = None
) -> dict[str, str | int | bool]:
    """Build bounded masked observation attributes; generic instrumentation never calls this."""
    attributes: dict[str, str | int | bool] = {}
    if input_value is not None:
        value, truncated, original_bytes, omitted = masked_json_snapshot(input_value)
        if value is None:
            attributes["kira.observation.input.content_omitted"] = omitted or "masking_error"
        else:
            attributes[OBSERVATION_INPUT] = value
            attributes["kira.observation.input.truncated"] = truncated
            attributes["kira.observation.input.original_bytes"] = original_bytes
    if output_value is not None:
        value, truncated, original_bytes, omitted = masked_json_snapshot(output_value)
        if value is None:
            attributes["kira.observation.output.content_omitted"] = omitted or "masking_error"
        else:
            attributes[OBSERVATION_OUTPUT] = value
            attributes["kira.observation.output.truncated"] = truncated
            attributes["kira.observation.output.original_bytes"] = original_bytes
    return attributes


def model_attribute(model: object) -> dict[str, str]:
    """Return a bounded model attribute or no attribute for unsafe input."""
    safe_model = safe_log_value(model)
    return {OBSERVATION_MODEL: safe_model} if isinstance(safe_model, str) else {}


def usage_attributes(usage: object) -> dict[str, str | int]:
    """Return only nonnegative provider-reported token counts."""
    if not isinstance(usage, Mapping):
        return {}
    normalized: dict[str, int] = {}
    for name in ("input", "output", "total"):
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            normalized[name] = value
    if not normalized:
        return {}
    attributes: dict[str, str | int] = {
        OBSERVATION_USAGE: json.dumps(normalized, separators=(",", ":"))
    }
    if "input" in normalized:
        attributes[GEN_AI_INPUT_TOKENS] = normalized["input"]
    if "output" in normalized:
        attributes[GEN_AI_OUTPUT_TOKENS] = normalized["output"]
    return attributes
