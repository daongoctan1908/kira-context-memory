"""Request-bound session, Origin and CSRF validation."""

import hmac

from fastapi import Request

from app.application.services.auth import AuthService
from app.config.settings import Settings
from app.domain.errors.auth import InvalidCsrfTokenError, InvalidSessionError
from app.domain.models.auth import AuthSession
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.ports.identity import IdentityPort


def _origin(settings: Settings) -> str:
    if settings.auth_allowed_origin is None:
        raise InvalidCsrfTokenError
    return str(settings.auth_allowed_origin).rstrip("/")


def require_allowed_origin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    supplied = request.headers.get("origin", "").rstrip("/")
    if not supplied or not hmac.compare_digest(supplied, _origin(settings)):
        raise InvalidCsrfTokenError


async def resolve_auth_session(request: Request, *, require_csrf: bool) -> AuthSession:
    settings: Settings = request.app.state.settings
    service: AuthService | None = request.app.state.auth_service
    if service is None:
        raise InvalidSessionError
    token = request.cookies.get(settings.auth_session_cookie_name, "")
    session = await service.resolve(token)
    if require_csrf:
        require_allowed_origin(request)
        await service.verify_csrf(session, request.headers.get("x-csrf-token", ""))
    request.state.auth_session = session
    return session


async def resolve_chat_principal(request: Request) -> AuthenticatedPrincipal | None:
    settings: Settings = request.app.state.settings
    if settings.auth_enabled:
        return (await resolve_auth_session(request, require_csrf=True)).principal
    identity: IdentityPort = request.app.state.identity_provider
    principal = await identity.resolve()
    if settings.app_environment == "production" and principal is None:
        raise InvalidSessionError
    return principal
