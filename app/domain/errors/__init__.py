"""Typed errors exposed by outbound domain ports."""

from app.domain.errors.kira import (
    KiraAuthenticationError,
    KiraClientError,
    KiraConnectionError,
    KiraHttpError,
    KiraMalformedSseError,
    KiraProtocolError,
    KiraTimeoutError,
)

__all__ = [
    "KiraAuthenticationError",
    "KiraClientError",
    "KiraConnectionError",
    "KiraHttpError",
    "KiraMalformedSseError",
    "KiraProtocolError",
    "KiraTimeoutError",
]
