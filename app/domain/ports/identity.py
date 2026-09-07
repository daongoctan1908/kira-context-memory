"""Port for resolving a trusted end-user principal."""

from typing import Protocol

from app.domain.models.identity import AuthenticatedPrincipal


class IdentityPort(Protocol):
    async def resolve(self) -> AuthenticatedPrincipal | None:
        """Return the request principal or ``None`` when identity is unavailable."""
        ...
