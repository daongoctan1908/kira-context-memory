"""Application-owned correlation for every public chat attempt."""

from collections.abc import Callable
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.infrastructure.observability.context import bind_observability_context

CorrelationIdFactory = Callable[[], str]


class CorrelationMiddleware:
    """Create correlation before validation and preserve the public response header."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        id_factory: CorrelationIdFactory | None = None,
    ) -> None:
        self._app = app
        self._id_factory = id_factory or (lambda: uuid4().hex)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_chat_request(scope):
            await self._app(scope, receive, send)
            return

        correlation_id = self._id_factory()
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Correlation-ID"] = correlation_id
            await send(message)

        with bind_observability_context(correlation_id=correlation_id):
            await self._app(scope, receive, send_with_correlation)


def _is_chat_request(scope: Scope) -> bool:
    return (
        scope["type"] == "http"
        and scope.get("method") == "POST"
        and scope.get("path") == "/chat"
    )
