"""Product SSE acceptance for fenced persistence and idempotent replay."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx

from app.application.services.chat_idempotency import hash_chat_content
from app.config.settings import Settings
from app.domain.errors.auth import InvalidCsrfTokenError, InvalidSessionError
from app.domain.errors.conversation import ChatRequestConflictError
from app.domain.errors.kira import KiraConnectionError
from app.domain.models.auth import AuthSession, IssuedSession
from app.domain.models.conversation import (
    AppendTurnResult,
    ChatRequestReservation,
    ChatRequestReservationOutcome,
    ChatRequestStatus,
    CompletedTurnReference,
    ConversationMessage,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraEventKind, KiraStreamEvent
from app.presentation.api.main import create_app

_USER_ID = UUID("11111111-1111-4111-8111-111111111111")
_AUTH_SESSION_ID = UUID("22222222-2222-4222-8222-222222222222")
_CONVERSATION_ID = UUID("33333333-3333-4333-8333-333333333333")
_NOW = datetime(2026, 9, 19, tzinfo=UTC)


class FakeAuthService:
    def _session(self) -> AuthSession:
        return AuthSession(
            session_id=_AUTH_SESSION_ID,
            principal=AuthenticatedPrincipal(str(_USER_ID)),
            username="alice",
            csrf_token_hash=b"c" * 32,
            absolute_expires_at=_NOW + timedelta(hours=8),
        )

    async def login(self, _username: str, _password: str) -> IssuedSession:
        session = self._session()
        return IssuedSession(
            session.session_id,
            session.principal,
            session.username,
            "session-alice",
            "csrf-token",
            session.absolute_expires_at,
        )

    async def resolve(self, token: str) -> AuthSession:
        if token != "session-alice":
            raise InvalidSessionError
        return self._session()

    async def verify_csrf(self, _session: AuthSession, token: str) -> None:
        if token != "csrf-token":
            raise InvalidCsrfTokenError


class ListStream(AsyncIterator[KiraStreamEvent]):
    def __init__(self, *, fail: bool = False) -> None:
        self._events = iter(
            (
                KiraStreamEvent(KiraEventKind.TEXT, "{}", {}, "xin "),
                KiraStreamEvent(KiraEventKind.TEXT, "{}", {}, "chao"),
            )
        )
        self._fail = fail

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            if self._fail:
                self._fail = False
                raise KiraConnectionError from None
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        return None


class FakeKira:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_stream = False

    async def chat_stream(self, _message: str):
        self.calls += 1
        return ListStream(fail=self.fail_stream)


class FakeRewriter:
    async def rewrite(self, context):
        return context.current_query


class FakeStore:
    def __init__(self) -> None:
        self.reservation: ChatRequestReservation | None = None
        self.content_hash: bytes | None = None
        self.messages: tuple[ConversationMessage, ConversationMessage] | None = None
        self.fail_complete = False
        self.abandoned: ChatRequestStatus | None = None

    async def reserve_chat_request(
        self,
        user_id,
        session_id,
        client_message_id,
        content_hash,
        *,
        now,
        lease_seconds,
    ):
        if user_id != str(_USER_ID) or session_id != "public-session":
            return None
        if self.reservation is not None:
            if content_hash != self.content_hash:
                raise ChatRequestConflictError
            status = self.reservation.status
            outcome = (
                ChatRequestReservationOutcome.COMPLETED
                if status is ChatRequestStatus.COMPLETED
                else ChatRequestReservationOutcome.IN_PROGRESS
            )
            return ChatRequestReservation(
                self.reservation.request_id,
                self.reservation.conversation_id,
                self.reservation.client_message_id,
                self.reservation.turn_id,
                status,
                outcome,
                self.reservation.attempt_count,
                None,
                None,
            )
        self.content_hash = content_hash
        self.reservation = ChatRequestReservation(
            uuid4(),
            _CONVERSATION_ID,
            client_message_id,
            "turn-product",
            ChatRequestStatus.PROCESSING,
            ChatRequestReservationOutcome.ACQUIRED,
            1,
            uuid4(),
            now + timedelta(seconds=lease_seconds),
        )
        return self.reservation

    async def complete_chat_request(
        self,
        user_id,
        reservation,
        user_message,
        assistant_message,
        **_kwargs,
    ):
        if self.fail_complete:
            raise RuntimeError("private database detail")
        assert user_id == str(_USER_ID)
        assert reservation.lease_token == self.reservation.lease_token
        self.messages = (user_message, assistant_message)
        self.reservation = ChatRequestReservation(
            reservation.request_id,
            reservation.conversation_id,
            reservation.client_message_id,
            reservation.turn_id,
            ChatRequestStatus.COMPLETED,
            ChatRequestReservationOutcome.COMPLETED,
            reservation.attempt_count,
            None,
            None,
        )
        return AppendTurnResult(
            True,
            CompletedTurnReference(
                user_id,
                user_message.session_id,
                reservation.conversation_id,
                reservation.turn_id,
                2,
            ),
        )

    async def read_completed_chat_request(self, user_id, session_id, reservation):
        assert user_id == str(_USER_ID)
        assert session_id == "public-session"
        assert reservation.status is ChatRequestStatus.COMPLETED
        return self.messages

    async def abandon_chat_request(self, request_id, lease_token, *, status, now):
        del request_id, lease_token, now
        self.abandoned = status
        return True

    async def read_recent(self, *_args):
        return self.messages or ()


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
    store = FakeStore()
    kira = FakeKira()
    app = create_app(
        settings=_settings(),
        kira_client=kira,  # type: ignore[arg-type]
        conversation_store=store,  # type: ignore[arg-type]
        query_rewriter=FakeRewriter(),
        auth_service=FakeAuthService(),  # type: ignore[arg-type]
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
            base_url="https://chat.test",
        ) as client:
            await client.post(
                "/api/v1/auth/login",
                headers={"Origin": "https://chat.test"},
                json={"username": "alice", "password": "password-secret"},
            )
            yield client, store, kira


def _headers() -> dict[str, str]:
    return {"Origin": "https://chat.test", "X-CSRF-Token": "csrf-token"}


async def test_product_stream_commits_before_completed_and_duplicate_replays() -> None:
    async with _client() as (client, store, kira):
        client_message_id = uuid4()
        body = {"client_message_id": str(client_message_id), "message": "hello"}
        response = await client.post(
            "/api/v1/conversations/public-session/messages",
            headers=_headers(),
            json=body,
        )
        assert response.status_code == 200
        assert (
            response.text.index("event: message.started")
            < response.text.index("event: message.delta")
            < response.text.index("event: message.completed")
        )
        assert store.messages is not None
        assert store.messages[1].content == "xin chao"
        assert kira.calls == 1

        replay = await client.post(
            "/api/v1/conversations/public-session/messages",
            headers=_headers(),
            json=body,
        )
        assert replay.status_code == 200
        assert '"replayed":true' in replay.text
        assert "xin chao" in replay.text
        assert kira.calls == 1


async def test_product_idempotency_conflict_and_in_progress_are_409() -> None:
    async with _client() as (client, _store, _kira):
        client_message_id = uuid4()
        first = await client.post(
            "/api/v1/conversations/public-session/messages",
            headers=_headers(),
            json={"client_message_id": str(client_message_id), "message": "hello"},
        )
        assert first.status_code == 200
        conflict = await client.post(
            "/api/v1/conversations/public-session/messages",
            headers=_headers(),
            json={"client_message_id": str(client_message_id), "message": "different"},
        )
        assert conflict.status_code == 409

    async with _client() as (client, store, _kira):
        client_message_id = uuid4()
        await store.reserve_chat_request(
            str(_USER_ID),
            "public-session",
            client_message_id,
            hash_chat_content("hello"),
            now=datetime.now(UTC),
            lease_seconds=120,
        )
        in_progress = await client.post(
            "/api/v1/conversations/public-session/messages",
            headers=_headers(),
            json={"client_message_id": str(client_message_id), "message": "hello"},
        )
        assert in_progress.status_code == 409
        assert in_progress.json()["detail"] == "request_in_progress"


async def test_stream_or_completion_failure_never_emits_completed() -> None:
    async with _client() as (client, store, kira):
        kira.fail_stream = True
        response = await client.post(
            "/api/v1/conversations/public-session/messages",
            headers=_headers(),
            json={"client_message_id": str(uuid4()), "message": "hello"},
        )
        assert "event: message.failed" in response.text
        assert "event: message.completed" not in response.text
        assert store.messages is None
        assert store.abandoned is ChatRequestStatus.FAILED

    async with _client() as (client, store, _kira):
        store.fail_complete = True
        response = await client.post(
            "/api/v1/conversations/public-session/messages",
            headers=_headers(),
            json={"client_message_id": str(uuid4()), "message": "hello"},
        )
        assert "event: message.failed" in response.text
        assert "event: message.completed" not in response.text
        assert "private database detail" not in response.text
        assert store.messages is None
        assert store.abandoned is ChatRequestStatus.FAILED
