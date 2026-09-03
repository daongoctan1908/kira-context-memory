"""Client-facing SSE serialization and stream lifecycle handling."""

import logging
from collections.abc import AsyncIterator

from app.application.use_cases.handle_chat import ChatStreamSession
from app.domain.errors.kira import KiraClientError
from app.presentation.api.errors import encode_gateway_error_event

logger = logging.getLogger(__name__)


async def stream_gateway_events(
    session: ChatStreamSession,
    correlation_id: str,
) -> AsyncIterator[bytes]:
    """Proxy KiRa frames incrementally and close downstream on every exit path."""
    event_count = 0
    try:
        async for event in session:
            event_count += 1
            yield f"data: {event.raw_data}\n\n".encode()
    except KiraClientError as error:
        logger.warning(
            "KiRa stream failed",
            extra={
                "correlation_id": correlation_id,
                "error_code": error.code,
                "retryable": error.retryable,
            },
        )
        yield encode_gateway_error_event(error, correlation_id)
    except Exception as unexpected:
        logger.error(
            "Unexpected Gateway stream failure",
            extra={
                "correlation_id": correlation_id,
                "exception_type": type(unexpected).__name__,
            },
        )
        error = KiraClientError("Unexpected Gateway stream failure")
        yield encode_gateway_error_event(error, correlation_id)
    finally:
        await session.aclose()
        logger.info(
            "KiRa stream closed",
            extra={
                "correlation_id": correlation_id,
                "event_count": event_count,
                "assistant_character_count": len(session.final_text),
            },
        )
