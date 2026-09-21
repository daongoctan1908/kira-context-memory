"""HTTP contract tests for authenticated conversation management."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx

from app.config.settings import Settings
from app.domain.errors.auth import InvalidCsrfTokenError, InvalidSessionError
from app.domain.errors.conversation import ConversationStoreOperationError
from app.domain.models.auth import AuthSession, IssuedSession
from app.domain.models.conversation import (
    ConversationHistoryPage,
    ConversationListCursor,
    ConversationMessage,
    ConversationPage,
    ConversationRole,
    ConversationStatus,
    ConversationSummary,
    MessageFeedbackRating,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraStreamEvent
from app.presentation.api.main import create_app

_USER_A = UUID("11111111-1111-4111-8111-111111111111")
_USER_B = UUID("22222222-2222-4222-8222-222222222222")
_AUTH_SESSION = UUID("33333333-3333-4333-8333-333333333333")
_CONVERSATION_ID = UUID("44444444-4444-4444-8444-444444444444")
_NOW = datetime(2026, 9, 19, tzinfo=UTC)


class FakeAuthService:
    def _session(self, user_id: UUID) -> AuthSession:
        return AuthSession(
            session_id=_AUTH_SESSION,
            principal=AuthenticatedPrincipal(str(user_id)),
            username="alice" if user_id == _USER_A else "bob",
            csrf_token_hash=b"c" * 32,
            absolute_expires_at=_NOW + timedelta(hours=8),
        )

    async def login(self, username: str, _password: str) -> IssuedSession:
        user_id = _USER_A if username == "alice" else _USER_B
        session = self._session(user_id)
        return IssuedSession(
            session.session_id,
            session.principal,
            session.username,
            f"session-{session.username}",
            "csrf-token",
            session.absolute_expires_at,
        )

    async def resolve(self, token: str) -> AuthSession:
        if token == "session-alice":
            return self._session(_USER_A)
        if token == "session-bob":
            return self._session(_USER_B)
        raise InvalidSessionError

    async def verify_csrf(self, _session: AuthSession, token: str) -> None:
        if token != "csrf-token":
            raise InvalidCsrfTokenError


class FakeConversationStore:
    def __init__(self, *, purge_error: bool = False) -> None:
        self.owner = str(_USER_A)
        self.summary = ConversationSummary(
            _CONVERSATION_ID,
            "public-session",
            "Support",
            ConversationStatus.ACTIVE,
            _NOW,
            _NOW,
            None,
        )
        self.deleted = False
        self.purged = False
        self.purge_error = purge_error
        self.list_cursors: list[ConversationListCursor | None] = []
        self.list_queries: list[str | None] = []
        self.feedback: MessageFeedbackRating | None = None

    async def create_conversation(self, user_id, *, title=None):
        assert user_id == self.owner
        return ConversationSummary(
            self.summary.conversation_id,
            self.summary.session_id,
            title,
            self.summary.status,
            self.summary.created_at,
            self.summary.updated_at,
            self.summary.last_message_at,
        )

    async def list_conversations(self, user_id, *, limit, cursor=None, query=None):
        assert limit <= 100
        self.list_cursors.append(cursor)
        self.list_queries.append(query)
        if user_id != self.owner:
            return ConversationPage((), None)
        if cursor is not None:
            return ConversationPage((), None)
        return ConversationPage(
            (self.summary,),
            ConversationListCursor(self.summary.activity_at, self.summary.conversation_id),
        )

    async def rename_conversation(self, user_id, session_id, title):
        if user_id != self.owner or session_id != self.summary.session_id or self.deleted:
            return None
        self.summary = ConversationSummary(
            self.summary.conversation_id,
            self.summary.session_id,
            title,
            self.summary.status,
            self.summary.created_at,
            self.summary.updated_at,
            self.summary.last_message_at,
        )
        return self.summary

    async def read_history(self, user_id, session_id, *, limit, before_message_id=None):
        assert limit <= 100
        if user_id != self.owner or session_id != self.summary.session_id or self.deleted:
            return None
        return ConversationHistoryPage(
            (
                ConversationMessage(
                    session_id,
                    "turn-1",
                    ConversationRole.USER,
                    "hello",
                    _NOW,
                ),
                ConversationMessage(
                    session_id,
                    "turn-1",
                    ConversationRole.ASSISTANT,
                    "xin chao",
                    _NOW,
                    feedback=self.feedback,
                ),
            ),
            41 if before_message_id is None else None,
        )

    async def set_message_feedback(self, user_id, session_id, turn_id, rating):
        if (
            user_id != self.owner
            or session_id != self.summary.session_id
            or turn_id != "turn-1"
            or self.deleted
        ):
            return False
        self.feedback = rating
        return True

    async def clear_message_feedback(self, user_id, session_id, turn_id):
        if (
            user_id != self.owner
            or session_id != self.summary.session_id
            or turn_id != "turn-1"
            or self.deleted
        ):
            return False
        self.feedback = None
        return True

    async def mark_deletion_pending(self, user_id, session_id):
        if user_id != self.owner or session_id != self.summary.session_id:
            return False
        self.deleted = True
        return True

    async def purge_deletion_pending(self, user_id, session_id):
        if self.purge_error:
            raise ConversationStoreOperationError
        if user_id != self.owner or session_id != self.summary.session_id or not self.deleted:
            return False
        self.purged = True
        return True

    async def read_recent(self, *_args):
        return ()

    async def is_conversation_active(self, *_args):
        return True

    async def append_turn(self, *_args, **_kwargs):
        raise AssertionError("conversation API test must not append a chat turn")

    async def read_through_boundary(self, *_args):
        return ()


class EmptyStream(AsyncIterator[KiraStreamEvent]):
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeKira:
    async def chat_stream(self, _message):
        return EmptyStream()


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
async def _client(*, purge_error: bool = False):
    store = FakeConversationStore(purge_error=purge_error)
    app = create_app(
        settings=_settings(),
        kira_client=FakeKira(),  # type: ignore[arg-type]
        conversation_store=store,  # type: ignore[arg-type]
        query_rewriter=FakeRewriter(),
        auth_service=FakeAuthService(),  # type: ignore[arg-type]
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://chat.test",
        ) as client:
            yield client, store


async def _login(client: httpx.AsyncClient, username: str = "alice") -> None:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://chat.test"},
        json={"username": username, "password": "password-secret"},
    )
    assert response.status_code == 200


def _unsafe_headers() -> dict[str, str]:
    return {"Origin": "https://chat.test", "X-CSRF-Token": "csrf-token"}


async def test_create_list_and_history_require_session_and_csrf() -> None:
    async with _client() as (client, store):
        assert (await client.get("/api/v1/conversations")).status_code == 401
        await _login(client)
        assert (
            await client.post("/api/v1/conversations", json={"title": "Support"})
        ).status_code == 403

        created = await client.post(
            "/api/v1/conversations",
            headers=_unsafe_headers(),
            json={"title": "  New support request  "},
        )
        assert created.status_code == 201
        assert created.json()["title"] == "New support request"
        assert created.json()["session_id"] == "public-session"

        listed = await client.get("/api/v1/conversations?limit=1")
        assert listed.status_code == 200
        assert [item["session_id"] for item in listed.json()["items"]] == ["public-session"]
        cursor = listed.json()["next_cursor"]
        assert cursor
        exhausted = await client.get(
            "/api/v1/conversations",
            params={"limit": 1, "cursor": cursor},
        )
        assert exhausted.status_code == 200
        assert exhausted.json()["items"] == []
        assert store.list_cursors[-1] == ConversationListCursor(
            store.summary.activity_at,
            store.summary.conversation_id,
        )

        history = await client.get("/api/v1/conversations/public-session/messages")
        assert history.status_code == 200
        assert history.json()["items"][0]["content"] == "hello"
        assert history.json()["next_before_message_id"] == 41


async def test_two_user_isolation_invalid_cursor_and_pending_delete() -> None:
    async with _client() as (client, store):
        await _login(client, "bob")
        assert (await client.get("/api/v1/conversations")).json()["items"] == []
        assert (
            await client.get("/api/v1/conversations/public-session/messages")
        ).status_code == 404
        assert (
            await client.patch(
                "/api/v1/conversations/public-session",
                headers=_unsafe_headers(),
                json={"title": "Not mine"},
            )
        ).status_code == 404
        assert (
            await client.delete("/api/v1/conversations/public-session", headers=_unsafe_headers())
        ).status_code == 204

        await _login(client, "alice")
        malformed = await client.get("/api/v1/conversations?cursor=not-a-cursor")
        assert malformed.status_code == 422
        assert malformed.json()["code"] == "CONVERSATION_CURSOR_INVALID"
        assert (await client.delete("/api/v1/conversations/public-session")).status_code == 403
        deleted = await client.delete(
            "/api/v1/conversations/public-session",
            headers=_unsafe_headers(),
        )
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert store.purged is True
        repeated = await client.delete(
            "/api/v1/conversations/public-session",
            headers=_unsafe_headers(),
        )
        assert repeated.status_code == 204
        assert (
            await client.get("/api/v1/conversations/public-session/messages")
        ).status_code == 404


async def test_delete_failure_keeps_pending_and_returns_sanitized_retry_contract() -> None:
    async with _client(purge_error=True) as (client, store):
        await _login(client, "alice")
        response = await client.delete(
            "/api/v1/conversations/public-session",
            headers=_unsafe_headers(),
        )

        assert response.status_code == 503
        assert response.json()["code"] == "DELETION_RETRY_REQUIRED"
        assert response.json()["retryable"] is True
        assert store.deleted is True
        assert store.purged is False
        assert (
            await client.get("/api/v1/conversations/public-session/messages")
        ).status_code == 404


async def test_conversation_request_validation_is_bounded() -> None:
    async with _client() as (client, _store):
        await _login(client)
        assert (
            await client.post(
                "/api/v1/conversations",
                headers=_unsafe_headers(),
                json={"title": "x" * 201},
            )
        ).status_code == 422
        assert (await client.get("/api/v1/conversations?limit=101")).status_code == 422
        oversized_search = await client.get(
            "/api/v1/conversations",
            params={"q": "x" * 101},
        )
        assert oversized_search.status_code == 422
        assert (
            await client.get("/api/v1/conversations/public-session/messages?before_message_id=0")
        ).status_code == 422


async def test_rename_search_and_feedback_are_owned_csrf_protected_contracts() -> None:
    async with _client() as (client, store):
        await _login(client)

        searched = await client.get("/api/v1/conversations", params={"q": "  Support  "})
        assert searched.status_code == 200
        assert store.list_queries[-1] == "  Support  "

        assert (
            await client.patch(
                "/api/v1/conversations/public-session",
                json={"title": "Renamed"},
            )
        ).status_code == 403
        renamed = await client.patch(
            "/api/v1/conversations/public-session",
            headers=_unsafe_headers(),
            json={"title": "  Renamed support  "},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Renamed support"

        rated = await client.put(
            "/api/v1/conversations/public-session/messages/turn-1/feedback",
            headers=_unsafe_headers(),
            json={"rating": "up"},
        )
        assert rated.status_code == 200
        assert rated.json() == {"turn_id": "turn-1", "rating": "up"}
        assert store.feedback is MessageFeedbackRating.UP
        history = await client.get("/api/v1/conversations/public-session/messages")
        assert history.json()["items"][1]["feedback"] == "up"

        cleared = await client.delete(
            "/api/v1/conversations/public-session/messages/turn-1/feedback",
            headers=_unsafe_headers(),
        )
        assert cleared.status_code == 204
        assert store.feedback is None
        missing = await client.put(
            "/api/v1/conversations/public-session/messages/missing/feedback",
            headers=_unsafe_headers(),
            json={"rating": "down"},
        )
        assert missing.status_code == 404
