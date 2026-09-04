"""HTTPX implementation of the external KiRa client port."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any, Self

import anyio
import httpx

from app.config.settings import Settings
from app.domain.errors.kira import (
    KiraAuthenticationError,
    KiraConnectionError,
    KiraHttpError,
    KiraProtocolError,
    KiraTimeoutError,
)
from app.domain.models.kira import KiraAuthResult, KiraStreamEvent
from app.infrastructure.kira.sse_parser import parse_sse_line
from app.infrastructure.kira.token_manager import Clock, KiraTokenManager


class KiraHttpAdapter:
    """Translate the confirmed KiRa HTTP/SSE contract into domain events."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._http_client = http_client
        self._settings = settings
        self._timeout = httpx.Timeout(
            connect=settings.kira_connect_timeout_seconds,
            read=settings.kira_read_timeout_seconds,
            write=settings.kira_connect_timeout_seconds,
            pool=settings.kira_connect_timeout_seconds,
        )
        token_manager_kwargs: dict[str, Any] = {
            "expiry_skew_seconds": settings.kira_token_expiry_skew_seconds,
        }
        if clock is not None:
            token_manager_kwargs["clock"] = clock
        self._token_manager = KiraTokenManager(self.authenticate, **token_manager_kwargs)

    async def authenticate(self) -> KiraAuthResult:
        """Call KiRa ``POST /authenticate`` and parse its runtime token."""
        try:
            response = await self._http_client.post(
                self._url("authenticate"),
                headers=self._headers(),
                json={
                    "username": self._settings.kira_username,
                    "domain": self._settings.kira_domain,
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise KiraTimeoutError(stage="authentication") from exc
        except httpx.RequestError as exc:
            raise KiraConnectionError from exc

        if not response.is_success:
            raise KiraHttpError(status_code=response.status_code)

        try:
            body = response.json()
        except ValueError as exc:
            raise KiraProtocolError from exc
        if not isinstance(body, dict):
            raise KiraProtocolError

        error_code = body.get("errorCode")
        if error_code != "00":
            downstream_code = error_code if isinstance(error_code, str) else None
            raise KiraAuthenticationError(downstream_code=downstream_code)

        token = body.get("content")
        if not isinstance(token, str) or not token.strip():
            raise KiraAuthenticationError(downstream_code="00")

        token_expiration_time = _optional_number(body.get("tokenExpirationTime"))
        return KiraAuthResult(
            token=token,
            token_expiration_time=token_expiration_time,
        )

    async def chat_stream(self, message: str) -> AsyncIterator[KiraStreamEvent]:
        """Open a KiRa chat request and return an incremental event iterator."""
        token = await self._token_manager.get_token()
        stream_context, response = await self._open_chat_stream(message=message, token=token)

        if response.status_code in {401, 403}:
            await stream_context.__aexit__(None, None, None)
            await self._token_manager.invalidate(expected_token=token)
            token = await self._token_manager.get_token()
            stream_context, response = await self._open_chat_stream(message=message, token=token)

        if not response.is_success:
            status_code = response.status_code
            await stream_context.__aexit__(None, None, None)
            raise KiraHttpError(status_code=status_code)

        return _KiraResponseStream(stream_context, response)

    async def invalidate_token(self) -> None:
        """Explicitly invalidate the in-process token cache."""
        await self._token_manager.invalidate()

    async def _open_chat_stream(
        self,
        *,
        message: str,
        token: str,
    ) -> tuple[AbstractAsyncContextManager[httpx.Response], httpx.Response]:
        stream_context = self._http_client.stream(
            "POST",
            self._url("api/v1/chat"),
            headers=self._headers(),
            json={
                "sender": {
                    "data": None,
                    "domain": self._settings.kira_domain,
                    "device": self._settings.kira_device,
                },
                "service": self._settings.kira_service_id,
                "content": None,
                "message": {
                    "text": message,
                    "type": self._settings.kira_message_type,
                },
                "token": token,
                "stream": True,
            },
            timeout=self._timeout,
        )
        try:
            response = await stream_context.__aenter__()
        except httpx.TimeoutException as exc:
            raise KiraTimeoutError(stage="chat connection") from exc
        except httpx.RequestError as exc:
            raise KiraConnectionError from exc
        return stream_context, response

    def _url(self, path: str) -> str:
        return str(self._settings.kira_base_url).rstrip("/") + "/" + path.lstrip("/")

    def _headers(self) -> dict[str, str]:
        credential = self._settings.kira_basic_auth.get_secret_value()
        return {
            "Authorization": f"Basic {credential}",
            "Content-Type": "application/json",
        }


class _KiraResponseStream(AsyncIterator[KiraStreamEvent]):
    """Own the HTTP stream until exhaustion, error or explicit client cancellation."""

    def __init__(
        self,
        stream_context: AbstractAsyncContextManager[httpx.Response],
        response: httpx.Response,
    ) -> None:
        self._stream_context = stream_context
        self._lines = response.aiter_lines().__aiter__()
        self._closed = False

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> KiraStreamEvent:
        if self._closed:
            raise StopAsyncIteration

        while True:
            try:
                line = await anext(self._lines)
                event = parse_sse_line(line)
            except StopAsyncIteration:
                await self.aclose()
                raise
            except httpx.TimeoutException as exc:
                await self.aclose()
                raise KiraTimeoutError(stage="chat stream") from exc
            except httpx.RequestError as exc:
                await self.aclose()
                raise KiraConnectionError from exc
            except BaseException:
                await self.aclose()
                raise

            if event is not None:
                return event

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # ASGI disconnect cancels the surrounding AnyIO scope. Shield only release
        # of the HTTP connection so cancellation cannot leak a checked-out stream.
        with anyio.CancelScope(shield=True):
            await self._stream_context.__aexit__(None, None, None)


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
