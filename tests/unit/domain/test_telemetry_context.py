import json

import pytest

from app.domain.models.telemetry_context import (
    MAX_TELEMETRY_CONTEXT_BYTES,
    TelemetryContext,
    parse_telemetry_context,
    serialize_telemetry_context,
)

CORRELATION_ID = "0123456789abcdef0123456789abcdef"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_version_one_carrier_serializes_only_allowlisted_fields() -> None:
    value = TelemetryContext(
        correlation_id=CORRELATION_ID,
        traceparent=TRACEPARENT,
        tracestate="vendor=value",
    )

    assert serialize_telemetry_context(value) == {
        "version": 1,
        "correlation_id": CORRELATION_ID,
        "traceparent": TRACEPARENT,
        "tracestate": "vendor=value",
    }


def test_invalid_trace_does_not_discard_valid_correlation() -> None:
    parsed = parse_telemetry_context(
        {
            "version": 1,
            "correlation_id": CORRELATION_ID,
            "traceparent": "not-a-trace-header",
            "tracestate": "vendor=value",
        }
    )

    assert parsed == TelemetryContext(correlation_id=CORRELATION_ID)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not-an-object",
        [],
        {"version": 2, "correlation_id": CORRELATION_ID},
        {"version": 1, "correlation_id": "UPPERCASE00000000000000000000000"},
        {"version": 1, "traceparent": "00-" + ("0" * 32) + "-" + ("1" * 16) + "-01"},
    ],
)
def test_malformed_carrier_is_ignored(value: object) -> None:
    assert parse_telemetry_context(value) is None


def test_oversized_carrier_is_ignored_before_field_selection() -> None:
    value = {
        "version": 1,
        "correlation_id": CORRELATION_ID,
        "arbitrary": "x" * MAX_TELEMETRY_CONTEXT_BYTES,
    }

    assert len(json.dumps(value).encode()) > MAX_TELEMETRY_CONTEXT_BYTES
    assert parse_telemetry_context(value) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"correlation_id": "bad"},
        {"traceparent": "bad"},
        {"traceparent": TRACEPARENT, "tracestate": "bad=value=again"},
        {"version": True},
        {},
    ],
)
def test_constructor_rejects_invalid_generated_carriers(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        TelemetryContext(**changes)  # type: ignore[arg-type]


def test_serializer_is_fail_open_for_non_carrier_input() -> None:
    assert serialize_telemetry_context({"correlation_id": CORRELATION_ID}) is None
