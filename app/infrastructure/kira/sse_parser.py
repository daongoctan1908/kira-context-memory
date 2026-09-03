"""Defensive parser for KiRa's observed SSE-like ``data:`` frames."""

import json
from collections.abc import Mapping
from typing import Any

from app.domain.errors.kira import KiraMalformedSseError
from app.domain.models.kira import KiraEventKind, KiraStreamEvent

SSE_DATA_PREFIX = "data:"


def parse_sse_line(line: str) -> KiraStreamEvent | None:
    """Parse one KiRa stream line.

    Non-data lines are intentionally ignored. Structurally unknown but valid JSON frames
    are classified as ``UNKNOWN`` and retained for transparent proxying.
    """
    if not line.startswith(SSE_DATA_PREFIX):
        return None

    raw_data = line.removeprefix(SSE_DATA_PREFIX).lstrip()
    if not raw_data:
        raise KiraMalformedSseError

    try:
        payload = json.loads(raw_data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KiraMalformedSseError from exc

    if not isinstance(payload, Mapping):
        raise KiraMalformedSseError

    data = payload.get("data")
    data_mapping = data if isinstance(data, Mapping) else {}
    request_id = _optional_string(data_mapping.get("requestId"))
    message_id = _optional_string(data_mapping.get("messageId"))

    saw_status = False
    text_fragments: list[str] = []
    responses = data_mapping.get("response")
    if isinstance(responses, list):
        for response in responses:
            if not isinstance(response, Mapping):
                continue
            response_type = response.get("type")
            if response_type == KiraEventKind.STATUS:
                saw_status = True
            elif response_type == KiraEventKind.TEXT:
                text = _extract_text(response)
                if text is not None:
                    text_fragments.append(text)

    if text_fragments:
        kind = KiraEventKind.TEXT
    elif saw_status:
        kind = KiraEventKind.STATUS
    else:
        kind = KiraEventKind.UNKNOWN

    return KiraStreamEvent(
        kind=kind,
        raw_data=raw_data,
        payload=payload,
        text_fragment="".join(text_fragments) or None,
        request_id=request_id,
        message_id=message_id,
    )


def _extract_text(response: Mapping[str, Any]) -> str | None:
    content = response.get("content")
    if not isinstance(content, Mapping):
        return None
    return _optional_string(content.get("text"))


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
