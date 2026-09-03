"""Concurrency-safe in-process cache for KiRa runtime tokens."""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable

from app.domain.models.kira import KiraAuthResult

Authenticate = Callable[[], Awaitable[KiraAuthResult]]
Clock = Callable[[], float]


class KiraTokenManager:
    """Reuse a token while the configured TTL interpretation considers it valid.

    Week 1 deliberately treats ``tokenExpirationTime`` as a TTL in seconds. The cache
    uses monotonic time and refreshes early by ``expiry_skew_seconds``.
    """

    def __init__(
        self,
        authenticate: Authenticate,
        *,
        expiry_skew_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self._authenticate = authenticate
        self._expiry_skew_seconds = expiry_skew_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    async def get_token(self) -> str:
        """Return a cached valid token or authenticate once for concurrent callers."""
        if self._is_valid():
            return self._token_or_raise()

        async with self._lock:
            if self._is_valid():
                return self._token_or_raise()

            result = await self._authenticate()
            self._token = result.token
            self._expires_at = self._calculate_expiry(result.token_expiration_time)
            return result.token

    async def invalidate(self, expected_token: str | None = None) -> None:
        """Invalidate the cached token, optionally only when it matches the caller's token."""
        async with self._lock:
            if expected_token is None or expected_token == self._token:
                self._token = None
                self._expires_at = 0.0

    def _is_valid(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at

    def _calculate_expiry(self, ttl_seconds: float | None) -> float:
        if (
            ttl_seconds is None
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= self._expiry_skew_seconds
        ):
            return self._clock()
        return self._clock() + ttl_seconds - self._expiry_skew_seconds

    def _token_or_raise(self) -> str:
        if self._token is None:  # pragma: no cover - guarded by _is_valid
            raise RuntimeError("token cache invariant violated")
        return self._token
