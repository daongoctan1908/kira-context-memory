"""Fail-closed helpers for telemetry fields and optional AI content capture."""

import json
import math
import re
from collections.abc import Mapping, Sequence

_SECRET_NAMES = (
    r"authorization|api[_-]?key|password|secret|token|confidential|"
    r"(?:access|refresh|id|auth)[_-]?token|client[_-]?secret|"
    r"private[_-]?key|basic[_-]?auth|credentials?"
)
_IDENTIFIER_NAMES = (
    r"imsi|iccid|national[_-]?id|account[_-]?id|subscriber[_-]?id|"
    r"contract[_-]?id|payment[_-]?id"
)
_ASSIGNMENT_VALUE = (
    r'("(?:\\.|[^"\\])*(?:"|\\?\Z)|\'(?:\\.|[^\'\\])*(?:\'|\\?\Z)|'
    r"[\[{][\s\S]*|[^\s,;}\]]+)"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)\b({_SECRET_NAMES})\b([\"']?\s*[:=]\s*)"
    rf"(?:(?:Bearer|Basic)\s+)?{_ASSIGNMENT_VALUE}"
)
_IDENTIFIER_ASSIGNMENT = re.compile(
    rf"(?i)\b({_IDENTIFIER_NAMES})\b([\"']?\s*[:=]\s*){_ASSIGNMENT_VALUE}"
)
_SECRET_FIELD = re.compile(rf"(?i)^(?:{_SECRET_NAMES})$")
_IDENTIFIER_FIELD = re.compile(rf"(?i)^(?:{_IDENTIFIER_NAMES})$")
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
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
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
        redacted = _mask_embedded_json(value, max_length)
        # Stream snapshots can end inside a PEM block. Mask to the end of the
        # captured prefix, before assignment masking can consume its BEGIN marker.
        redacted = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", redacted)
        redacted = _SECRET_ASSIGNMENT.sub(
            lambda match: _mask_assignment(match, "[REDACTED]"), redacted
        )
        redacted = _IDENTIFIER_ASSIGNMENT.sub(
            lambda match: _mask_assignment(match, "[REDACTED_ID]"), redacted
        )
        redacted = _DSN_CREDENTIALS.sub(r"\1://[REDACTED]:[REDACTED]@", redacted)
        redacted = _AUTH_SCHEME.sub(r"\1 [REDACTED]", redacted)
        redacted = _API_TOKEN.sub("[REDACTED_API_KEY]", redacted)
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
    """Serialize JSON-safe values after masking sensitive fields and text."""
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
        original_bytes = len(original_rendered.encode("utf-8"))
        rendered = json.dumps(
            _mask_json_value(value, 0, max_length * 16),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        return None, False, 0, "masking_error"
    if len(rendered) <= max_length:
        return rendered, False, original_bytes, None
    # A sliced JSON object is not parseable by consumers. Preserve a masked
    # preview as a JSON string, accounting for quotes and escaped characters.
    marker = "[TRUNCATED]"
    if max_length < len(json.dumps(marker)):
        return None, True, original_bytes, "content_limit"
    start, end = 0, len(rendered)
    while start < end:
        middle = (start + end + 1) // 2
        candidate = json.dumps(rendered[:middle] + marker, ensure_ascii=False)
        if len(candidate) <= max_length:
            start = middle
        else:
            end = middle - 1
    return json.dumps(rendered[:start] + marker, ensure_ascii=False), True, original_bytes, None


def _mask_assignment(match: re.Match[str], replacement: str) -> str:
    """Preserve quoted values so embedded JSON remains well formed after masking."""
    value = match.group(3)
    quote = value[0] if value.startswith(('"', "'")) else ""
    if not quote and match.group(2).startswith(('"', "'")):
        quote = match.group(2)[0]
    return f"{match.group(1)}{match.group(2)}{quote}{replacement}{quote}"


def _mask_embedded_json(value: str, string_max_length: int) -> str:
    """Apply field-based masking to JSON objects and arrays embedded in prompts."""
    decoder = json.JSONDecoder()
    parts: list[str] = []
    cursor = 0
    for match in re.finditer(r"[\[{]", value):
        if match.start() < cursor:
            continue
        try:
            parsed, end = decoder.raw_decode(value, match.start())
        except ValueError:
            continue
        parts.append(value[cursor : match.start()])
        parts.append(
            json.dumps(
                _mask_json_value(parsed, 0, string_max_length),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        cursor = end
    if not parts:
        return value
    parts.append(value[cursor:])
    return "".join(parts)


def _mask_json_value(item: object, depth: int, string_max_length: int) -> object:
    if depth > 4:
        return "[REDACTED]"
    if item is None or isinstance(item, bool):
        return item
    if isinstance(item, int):
        return "[REDACTED_ID]" if _LONG_NUMBER.fullmatch(str(abs(item))) else item
    if isinstance(item, float):
        return item if math.isfinite(item) else "[REDACTED]"
    if isinstance(item, str):
        return redact_text(item, max_length=string_max_length)
    if isinstance(item, Mapping):
        result: dict[str, object] = {}
        for key, child in list(item.items())[:50]:
            safe_key = safe_log_value(key)
            if isinstance(safe_key, str):
                if _SECRET_FIELD.fullmatch(safe_key):
                    result[safe_key] = "[REDACTED]"
                elif _IDENTIFIER_FIELD.fullmatch(safe_key):
                    result[safe_key] = "[REDACTED_ID]"
                else:
                    result[safe_key] = _mask_json_value(child, depth + 1, string_max_length)
        return result
    if isinstance(item, Sequence) and not isinstance(item, bytes | bytearray):
        return [_mask_json_value(child, depth + 1, string_max_length) for child in list(item)[:50]]
    return "[REDACTED]"
