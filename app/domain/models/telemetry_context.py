"""Small, framework-free carrier for the durable chat-to-Worker boundary."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

TELEMETRY_CONTEXT_VERSION = 1
MAX_TELEMETRY_CONTEXT_BYTES = 1024

_CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")
_TRACEPARENT = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_TRACESTATE_KEY = re.compile(
    r"^(?:[a-z0-9][a-z0-9_*/-]{0,255}|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z0-9][a-z0-9_*/-]{0,13})$"
)


@dataclass(frozen=True, slots=True)
class TelemetryContext:
    """Validated identifiers persisted with one memory job.

    Correlation and W3C context are independent so a damaged trace header never
    discards an otherwise useful application correlation ID.
    """

    correlation_id: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    version: int = TELEMETRY_CONTEXT_VERSION

    def __post_init__(self) -> None:
        if self.version != TELEMETRY_CONTEXT_VERSION or isinstance(self.version, bool):
            raise ValueError("unsupported telemetry context version")
        if self.correlation_id is not None and not _valid_correlation_id(self.correlation_id):
            raise ValueError("correlation_id must be 32 lowercase hexadecimal characters")
        if self.traceparent is not None and not _valid_traceparent(self.traceparent):
            raise ValueError("traceparent must be a valid W3C version 00 header")
        if self.tracestate is not None and (
            self.traceparent is None or not _valid_tracestate(self.tracestate)
        ):
            raise ValueError("tracestate requires valid bounded W3C trace context")
        if self.correlation_id is None and self.traceparent is None:
            raise ValueError("telemetry context must contain correlation or trace context")
        if len(_compact_json(self.to_mapping())) > MAX_TELEMETRY_CONTEXT_BYTES:
            raise ValueError("telemetry context exceeds the 1 KiB limit")

    def to_mapping(self) -> dict[str, object]:
        """Return only the allowlisted version 1 fields."""
        payload: dict[str, object] = {"version": self.version}
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        if self.traceparent is not None:
            payload["traceparent"] = self.traceparent
        if self.tracestate is not None:
            payload["tracestate"] = self.tracestate
        return payload


def parse_telemetry_context(value: object) -> TelemetryContext | None:
    """Sanitize an untrusted JSONB value without raising into queue processing."""
    if not isinstance(value, Mapping):
        return None
    try:
        if len(_compact_json(value)) > MAX_TELEMETRY_CONTEXT_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    if type(value.get("version")) is not int or value.get("version") != TELEMETRY_CONTEXT_VERSION:
        return None

    raw_correlation = value.get("correlation_id")
    correlation_id = raw_correlation if _valid_correlation_id(raw_correlation) else None
    raw_traceparent = value.get("traceparent")
    traceparent = raw_traceparent if _valid_traceparent(raw_traceparent) else None
    raw_tracestate = value.get("tracestate")
    tracestate = (
        raw_tracestate if traceparent is not None and _valid_tracestate(raw_tracestate) else None
    )
    if correlation_id is None and traceparent is None:
        return None
    try:
        return TelemetryContext(
            correlation_id=correlation_id,
            traceparent=traceparent,
            tracestate=tracestate,
        )
    except ValueError:
        return None


def serialize_telemetry_context(value: object) -> dict[str, object] | None:
    """Return a safe JSONB payload, or ``None`` when instrumentation supplied junk."""
    if not isinstance(value, TelemetryContext):
        return None
    try:
        payload = value.to_mapping()
        if len(_compact_json(payload)) > MAX_TELEMETRY_CONTEXT_BYTES:
            return None
        return payload
    except (TypeError, ValueError):
        return None


def _compact_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _valid_correlation_id(value: object) -> bool:
    return isinstance(value, str) and _CORRELATION_ID.fullmatch(value) is not None


def _valid_traceparent(value: object) -> bool:
    if not isinstance(value, str):
        return False
    matched = _TRACEPARENT.fullmatch(value)
    return bool(
        matched and matched.group("trace_id") != "0" * 32 and matched.group("span_id") != "0" * 16
    )


def _valid_tracestate(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    members = value.split(",")
    if len(members) > 32:
        return False
    seen: set[str] = set()
    for member in members:
        if member != member.strip() or "=" not in member:
            return False
        key, item_value = member.split("=", 1)
        if (
            _TRACESTATE_KEY.fullmatch(key) is None
            or key in seen
            or not item_value
            or len(item_value) > 256
            or item_value[0] == " "
            or item_value[-1] == " "
            or any(ord(character) < 0x20 or ord(character) > 0x7E for character in item_value)
            or "," in item_value
            or "=" in item_value
        ):
            return False
        seen.add(key)
    return True
