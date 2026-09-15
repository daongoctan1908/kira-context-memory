"""Fail-closed helpers for telemetry fields and optional AI content capture."""

import json
import math
import re
from collections.abc import Mapping, Sequence

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|token|confidential)\b"
    r"(\s*[:=]\s*)(?:(?:Bearer|Basic)\s+)?([^\s,;]+)"
)
_IDENTIFIER_ASSIGNMENT = re.compile(
    r"(?i)\b(imsi|iccid|national[_-]?id|account[_-]?id|subscriber[_-]?id|"
    r"contract[_-]?id|payment[_-]?id)\b(\s*[:=]\s*)([^\s,;]+)"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_API_TOKEN = re.compile(r"(?<!\w)(?:sk|pk)-[A-Za-z0-9_-]{16,}(?!\w)")
_DSN_CREDENTIALS = re.compile(
    r"(?i)\b(postgresql(?:\+[a-z0-9]+)?|postgres|mysql|redis)://([^\s:/@]+):([^\s@]+)@"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?84|0)(?:[ .-]?\d){8,10}(?!\w)")
_LONG_NUMBER = re.compile(r"(?<![\w.-])\d{9,20}(?![\w.-])")
_IPV4 = re.compile(r"(?<!\w)(?:\d{1,3}\.){3}\d{1,3}(?!\w)")
_MAC = re.compile(r"(?i)(?<!\w)(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?!\w)")
_JWT = re.compile(r"(?<!\w)eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?!\w)")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
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
        redacted = _IDENTIFIER_ASSIGNMENT.sub(r"\1\2[REDACTED_ID]", redacted)
        redacted = _DSN_CREDENTIALS.sub(r"\1://[REDACTED]:[REDACTED]@", redacted)
        redacted = _AUTH_SCHEME.sub(r"\1 [REDACTED]", redacted)
        redacted = _API_TOKEN.sub("[REDACTED_API_KEY]", redacted)
        redacted = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", redacted)
        redacted = _JWT.sub("[REDACTED_TOKEN]", redacted)
        redacted = _EMAIL.sub("[REDACTED_EMAIL]", redacted)
        redacted = _PHONE.sub("[REDACTED_PHONE]", redacted)
        redacted = _MAC.sub("[REDACTED_MAC]", redacted)
        redacted = _IPV4.sub("[REDACTED_IP]", redacted)
        redacted = _LONG_NUMBER.sub("[REDACTED_ID]", redacted)
        if len(redacted) > max_length:
            return redacted[:max_length] + "[TRUNCATED]"
        return redacted
    except Exception:
        return "[REDACTED]"


def masked_json(value: object, *, max_length: int = 4096) -> str:
    """Serialize a shallow JSON-safe value after recursively masking all strings."""
    try:
        rendered = json.dumps(
            _mask_json_value(value, 0, max_length * 4),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(rendered) > max_length:
            return json.dumps("[REDACTED_OVERSIZE]")
        return rendered
    except Exception:
        return json.dumps("[REDACTED]")


def masked_json_snapshot(
    value: object,
    *,
    max_length: int = 4096,
) -> tuple[str | None, bool, int, str | None]:
    """Return masked JSON plus deterministic truncation metadata."""
    try:
        original_rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        rendered = json.dumps(
            _mask_json_value(value, 0, max_length * 16),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        return None, False, 0, "masking_error"
    original_bytes = len(original_rendered.encode("utf-8"))
    if len(rendered) <= max_length:
        return rendered, False, original_bytes, None
    return rendered[:max_length] + "[TRUNCATED]", True, original_bytes, None


def _mask_json_value(item: object, depth: int, string_max_length: int) -> object:
    if depth > 4:
        return "[REDACTED]"
    if item is None or isinstance(item, bool | int):
        return item
    if isinstance(item, float):
        return item if math.isfinite(item) else "[REDACTED]"
    if isinstance(item, str):
        return redact_text(item, max_length=string_max_length)
    if isinstance(item, Mapping):
        result: dict[str, object] = {}
        for key, child in list(item.items())[:50]:
            safe_key = safe_log_value(key)
            if isinstance(safe_key, str):
                result[safe_key] = _mask_json_value(child, depth + 1, string_max_length)
        return result
    if isinstance(item, Sequence) and not isinstance(item, bytes | bytearray):
        return [_mask_json_value(child, depth + 1, string_max_length) for child in list(item)[:50]]
    return "[REDACTED]"
