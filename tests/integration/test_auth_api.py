"""HTTP contract tests for cookie auth, CSRF and chat identity propagation."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx

from app.config.settings import Settings
from app.domain.errors.auth import InvalidCsrfTokenError, InvalidSessionError
from app.domain.models.auth import AuthSession, IssuedSession
from app.domain.models.conversation import AppendTurnResult, CompletedTurnReference
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraEventKind, KiraStreamEvent
from app.presentation.api.main import create_app

_USER_ID = UUID("11111111-1111-4111-8111-111111111111")
_SESSION_ID = UUID("22222222-2222-4222-8222-222222222222")
_NOW = datetime(2026, 9, 19, tzinfo=UTC)


class FakeAuthService:
    def __init__(self) -> None:
        self.session = AuthSession(
            session_id=_SESSION_ID,
            principal=AuthenticatedPrincipal(str(_USER_ID)),
            username="alice",
            csrf_token_hash=b"c" * 32,
            absolute_expires_at=_NOW + timedelta(hours=8),
        )
        self.logins = []
        self.logouts = []
        self.password_changes = []

    async def login(self, username: str, password: str) -> IssuedSession:
        self.logins.append((username, password))
        return IssuedSession(
            session_id=_SESSION_ID,
            principal=self.session.principal,
            username="alice",
            session_token="session-token",
            csrf_token="csrf-token",
            absolute_expires_at=self.session.absolute_expires_at,
        )

    async def resolve(self, token: str) -> AuthSession:
        if token != "session-token":
            raise InvalidSessionError
        return self.session

    async def verify_csrf(self, session: AuthSession, token: str) -> None:
        if session != self.session or token != "csrf-token":
            raise InvalidCsrfTokenError

    async def logout(self, token: str) -> None:
        self.logouts.append(token)

    async def change_password(self, *values: str) -> None:
        self.password_changes.append(values)


class FakeConversationStore:
    def __init__(self) -> None:
        self.user_ids = []

    async def read_recent(self, user_id, _session_id, _limit):
        self.user_ids.append(user_id)
        return ()

    async def append_turn(
        self,
        user_id,
        user_message,
        _assistant_message,
        *,
        schedule_memory=False,
        telemetry_context=None,
    ):
        del schedule_memory, telemetry_context
        self.user_ids.append(user_id)
        return AppendTurnResult(
            inserted=True,
            reference=CompletedTurnReference(
                user_id=user_id,
                session_id=user_message.session_id,
                conversation_id=UUID("33333333-3333-4333-8333-333333333333"),
                turn_id=user_message.turn_id,
                boundary_message_id=2,
            ),
        )

    async def read_through_boundary(self, *_args):
        return ()


class OneEventStream(AsyncIterator[KiraStreamEvent]):
    def __init__(self) -> None:
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return KiraStreamEvent(
            kind=KiraEventKind.TEXT,
            raw_data='{"content":"ok"}',
            payload={"content": "ok"},
            text_fragment="ok",
        )

    async def aclose(self):
        return None


class FakeKira:
    async def chat_stream(self, _message):
        return OneEventStream()


class FakeRewriter:
    async def rewrite(self, context):
        return context.current_query


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="https://kira.test",
        kira_username="service-account",
        kira_basic_auth="secret",
        app_environment="production",
        database_url="postgresql://user:password@db/kira",
        auth_enabled=True,
        auth_allowed_origin="https://chat.test",
        auth_cookie_secure=True,
    )


@asynccontextmanager
async def _client():
    auth = FakeAuthService()
    conversations = FakeConversationStore()
    app = create_app(
        settings=_settings(),
        kira_client=FakeKira(),  # type: ignore[arg-type]
        conversation_store=conversations,
        query_rewriter=FakeRewriter(),
        auth_service=auth,  # type: ignore[arg-type]
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://chat.test",
        ) as client:
            yield client, auth, conversations


async def test_login_me_chat_and_logout_use_strict_host_cookies_and_csrf() -> None:
    async with _client() as (client, auth, conversations):
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://chat.test"},
            json={"username": "alice", "password": "password-secret"},
        )
        assert login.status_code == 200
        assert login.json() == {"user_id": str(_USER_ID), "username": "alice"}
        cookies = login.headers.get_list("set-cookie")
        assert len(cookies) == 2
        assert all("SameSite=strict" in value and "Secure" in value for value in cookies)
        assert any("__Host-kira_session=" in value and "HttpOnly" in value for value in cookies)
        assert any("__Host-kira_csrf=" in value and "HttpOnly" not in value for value in cookies)
        assert auth.logins == [("alice", "password-secret")]

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == "alice"

        chat = await client.post(
            "/chat",
            headers={"Origin": "https://chat.test", "X-CSRF-Token": "csrf-token"},
            json={"session_id": "conversation-1", "message": "hello"},
        )
        assert chat.status_code == 200
        assert str(_USER_ID) in conversations.user_ids

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://chat.test", "X-CSRF-Token": "csrf-token"},
        )
        assert logout.status_code == 204
        assert auth.logouts == ["session-token"]


async def test_origin_csrf_session_and_change_password_fail_closed() -> None:
    async with _client() as (client, auth, _conversations):
        no_origin = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "password-secret"},
        )
        assert no_origin.status_code == 403
        assert no_origin.json()["code"] == "AUTH_CSRF_INVALID"

        unauthenticated = await client.get("/api/v1/auth/me")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["code"] == "AUTH_SESSION_INVALID"

        await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://chat.test"},
            json={"username": "alice", "password": "password-secret"},
        )
        bad_csrf = await client.post(
            "/api/v1/auth/change-password",
            headers={"Origin": "https://chat.test", "X-CSRF-Token": "wrong"},
            json={"current_password": "old-password", "new_password": "new-password-123"},
        )
        assert bad_csrf.status_code == 403
        assert not auth.password_changes

        changed = await client.post(
            "/api/v1/auth/change-password",
            headers={"Origin": "https://chat.test", "X-CSRF-Token": "csrf-token"},
            json={"current_password": "old-password", "new_password": "new-password-123"},
        )
        assert changed.status_code == 204
        assert auth.password_changes == [
            ("session-token", "csrf-token", "old-password", "new-password-123")
        ]
