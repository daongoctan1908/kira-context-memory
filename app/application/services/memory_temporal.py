"""Add source timestamps to the request-local Mem0 extraction prompt."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS

DEFAULT_SOURCE_TIMEZONE = "Asia/Ho_Chi_Minh"

TEMPORAL_GUIDANCE = """Temporal grounding:

- source_time[i] is the trusted timestamp of New Message i.
- Resolve relative time using that message's source_time, not processing time.
- source_time is metadata, not a fact to memorize.
- If source_time is missing or ambiguous, do not invent a date.
"""


class TimedMessage(Protocol):
    @property
    def timestamp(self) -> datetime | None: ...


def build_memory_extraction_prompt(
    messages: Sequence[TimedMessage],
    *,
    source_timezone: str = DEFAULT_SOURCE_TIMEZONE,
    instructions: str = MEMORY_EXTRACTION_INSTRUCTIONS,
) -> str:
    try:
        zone = ZoneInfo(source_timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("memory source timezone must be a valid IANA timezone") from error

    source_times: list[str | None] = []

    for message in messages:
        timestamp = message.timestamp

        if timestamp is not None and (timestamp.tzinfo is None or timestamp.utcoffset() is None):
            raise ValueError("source timestamps must be timezone-aware")

        source_times.append(
            timestamp.astimezone(zone).isoformat() if timestamp is not None else None
        )

    temporal_context = json.dumps(
        {"source_time": source_times},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return f"{instructions}\n\n{TEMPORAL_GUIDANCE}\n{temporal_context}"
