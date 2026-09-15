"""Fail-closed helpers for telemetry fields and optional AI content capture."""

import json
import math
import re
from collections.abc import Mapping, Sequence

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|token)\b"
    r"(\s*[:=]\s*)(?:(?:Bearer|Basic)\s+)?([^\s,;]+)"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?84|0)(?:[ .-]?\d){8,10}(?!\w)")
_SAFE_FIELD = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def safe_log_value(value: object, *, max_length: int = 128) -> str | int | float | bool | None:
    """Accept bounded scalar log values without invoking arbitrary object formatting."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and len(value) <= max_length and _SAFE_FIELD.fullmatch(value):
        return value
    return None


def redact_text(value: object, *, max_length: int = 2048) -> str:
    """Mask common credentials and subscriber identifiers, failing closed on bad input."""
    if not isinstance(value, str):
        return "[REDACTED]"
    try:
        redacted = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
        redacted = _EMAIL.sub("[REDACTED_EMAIL]", redacted)
        redacted = _PHONE.sub("[REDACTED_PHONE]", redacted)
        if len(redacted) > max_length:
            return redacted[:max_length] + "[TRUNCATED]"
        return redacted
    except Exception:
        return "[REDACTED]"


def masked_json(value: object, *, max_length: int = 4096) -> str:
    """Serialize a shallow JSON-safe value after recursively masking all strings."""

    def mask(item: object, depth: int) -> object:
        if depth > 4:
            return "[REDACTED]"
        if item is None or isinstance(item, bool | int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else "[REDACTED]"
        if isinstance(item, str):
            return redact_text(item, max_length=1024)
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for key, child in list(item.items())[:50]:
                safe_key = safe_log_value(key)
                if isinstance(safe_key, str):
                    result[safe_key] = mask(child, depth + 1)
            return result
        if isinstance(item, Sequence) and not isinstance(item, bytes | bytearray):
            return [mask(child, depth + 1) for child in list(item)[:50]]
        return "[REDACTED]"

    try:
        rendered = json.dumps(mask(value, 0), ensure_ascii=False, separators=(",", ":"))
        if len(rendered) > max_length:
            return json.dumps("[REDACTED_OVERSIZE]")
        return rendered
    except Exception:
        return json.dumps("[REDACTED]")
