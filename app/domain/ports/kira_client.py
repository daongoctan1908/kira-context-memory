"""Port for the external KiRa API."""

from collections.abc import AsyncIterator
from typing import Protocol

from app.domain.models.kira import KiraAuthResult, KiraStreamEvent


class KiraClientPort(Protocol):
    """Application-facing contract implemented by a KiRa infrastructure adapter."""

    async def authenticate(self) -> KiraAuthResult:
        """Authenticate with KiRa and return a runtime token."""
        ...

    async def chat_stream(self, message: str) -> AsyncIterator[KiraStreamEvent]:
        """Open a KiRa chat stream and return its incremental events."""
        ...
