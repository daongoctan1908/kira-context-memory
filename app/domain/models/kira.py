"""Models crossing the KiRa client port boundary."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class KiraAuthResult:
    """Successful KiRa authentication result.

    ``token_expiration_time`` preserves the downstream field without assigning time
    semantics in the domain model. The infrastructure cache owns that interpretation.
    """

    token: str = field(repr=False)
    token_expiration_time: float | None = None

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("token must not be empty")


class KiraEventKind(StrEnum):
    """KiRa response variants observed in the baseline contract."""

    STATUS = "status_response"
    TEXT = "text"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class KiraStreamEvent:
    """One validated downstream ``data:`` event.

    ``raw_data`` keeps the JSON payload so the presentation layer can transparently
    proxy it. Parsed fields support correlation and final-text accumulation.
    """

    kind: KiraEventKind
    raw_data: str
    payload: Mapping[str, Any]
    text_fragment: str | None = None
    request_id: str | None = None
    message_id: str | None = None
