"""Concurrency-safe in-process cache for KiRa runtime tokens."""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Tracer

from app.domain.models.kira import KiraAuthResult
from app.infrastructure.observability.langfuse_attributes import OBSERVATION_TYPE
from app.infrastructure.observability.tracing import set_span_attribute, start_span

Authenticate = Callable[[], Awaitable[KiraAuthResult]]
Clock = Callable[[], float]


class KiraTokenManager:
    """Reuse a token while the configured TTL interpretation considers it valid.

    The KiRa contract treats ``tokenExpirationTime`` as a TTL in seconds. The cache
    uses monotonic time and refreshes early by ``expiry_skew_seconds``.
    """

    def __init__(
        self,
        authenticate: Authenticate,
        *,
        expiry_skew_seconds: float,
        clock: Clock = time.monotonic,
        tracer: Tracer | None = None,
    ) -> None:
        self._authenticate = authenticate
        self._expiry_skew_seconds = expiry_skew_seconds
        self._clock = clock
        self._tracer = tracer or trace.NoOpTracerProvider().get_tracer("app.infrastructure.kira")
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._has_authenticated = False

    async def get_token(self) -> str:
        """Return a cached valid token or authenticate once for concurrent callers."""
        with start_span(
            self._tracer,
            "kira.authenticate",
            kind=SpanKind.CLIENT,
            attributes={OBSERVATION_TYPE: "span"},
        ) as span:
            if self._is_valid():
                set_span_attribute(span, "kira.auth.cache_status", "hit")
                set_span_attribute(span, "kira.outcome", "success")
                return self._token_or_raise()

            cache_status = "refresh" if self._has_authenticated else "miss"
            set_span_attribute(span, "kira.auth.cache_status", cache_status)
            try:
                async with self._lock:
                    if self._is_valid():
                        set_span_attribute(span, "kira.auth.cache_status", "hit_after_wait")
                        set_span_attribute(span, "kira.outcome", "success")
                        return self._token_or_raise()

                    result = await self._authenticate()
                    self._token = result.token
                    self._expires_at = self._calculate_expiry(result.token_expiration_time)
                    self._has_authenticated = True
                    set_span_attribute(span, "kira.outcome", "success")
                    return result.token
            except BaseException:
                set_span_attribute(span, "kira.outcome", "error")
                raise

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
