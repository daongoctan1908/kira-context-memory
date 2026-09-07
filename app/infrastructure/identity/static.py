"""Week 3 identity adapters; real chatbot JWT authentication is deferred."""

from app.domain.models.identity import AuthenticatedPrincipal


class StaticIdentityAdapter:
    """Return one configured identity for controlled development and tests."""

    def __init__(self, user_id: str) -> None:
        self._principal = AuthenticatedPrincipal(user_id.strip())

    async def resolve(self) -> AuthenticatedPrincipal:
        return self._principal


class NullIdentityAdapter:
    """Fail closed for contextual capabilities while leaving KiRa available."""

    async def resolve(self) -> None:
        return None
