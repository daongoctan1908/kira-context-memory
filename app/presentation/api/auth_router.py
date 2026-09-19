"""Application-managed session authentication endpoints."""

from fastapi import APIRouter, Request, Response, status

from app.application.services.auth import AuthService
from app.config.settings import Settings
from app.domain.errors.auth import InvalidSessionError
from app.presentation.api.auth_dependencies import (
    require_allowed_origin,
    resolve_auth_session,
)
from app.presentation.schemas.auth import (
    AuthenticatedUserResponse,
    ChangePasswordRequest,
    LoginRequest,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_auth_cookies(
    response: Response,
    settings: Settings,
    *,
    session_token: str,
    csrf_token: str,
) -> None:
    common = {
        "secure": settings.auth_cookie_secure,
        "samesite": "strict",
        "path": "/",
        "max_age": settings.auth_session_absolute_seconds,
    }
    response.set_cookie(
        settings.auth_session_cookie_name,
        session_token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        csrf_token,
        httponly=False,
        **common,
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.auth_session_cookie_name, settings.auth_csrf_cookie_name):
        response.delete_cookie(
            name,
            path="/",
            secure=settings.auth_cookie_secure,
            httponly=name == settings.auth_session_cookie_name,
            samesite="strict",
        )


@router.post("/login", response_model=AuthenticatedUserResponse)
async def login(
    body: LoginRequest, request: Request, response: Response
) -> AuthenticatedUserResponse:
    require_allowed_origin(request)
    service: AuthService | None = request.app.state.auth_service
    if service is None:
        raise InvalidSessionError
    issued = await service.login(body.username, body.password.get_secret_value())
    settings: Settings = request.app.state.settings
    _set_auth_cookies(
        response,
        settings,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
    )
    return AuthenticatedUserResponse(
        user_id=issued.principal.user_id,
        username=issued.username,
    )


@router.get("/me", response_model=AuthenticatedUserResponse)
async def me(request: Request) -> AuthenticatedUserResponse:
    session = await resolve_auth_session(request, require_csrf=False)
    return AuthenticatedUserResponse(
        user_id=session.principal.user_id,
        username=session.username,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    await resolve_auth_session(request, require_csrf=True)
    settings: Settings = request.app.state.settings
    service: AuthService = request.app.state.auth_service
    await service.logout(request.cookies.get(settings.auth_session_cookie_name, ""))
    _clear_auth_cookies(response, settings)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
) -> None:
    await resolve_auth_session(request, require_csrf=True)
    settings: Settings = request.app.state.settings
    service: AuthService = request.app.state.auth_service
    await service.change_password(
        request.cookies.get(settings.auth_session_cookie_name, ""),
        request.headers.get("x-csrf-token", ""),
        body.current_password.get_secret_value(),
        body.new_password.get_secret_value(),
    )
    _clear_auth_cookies(response, settings)
