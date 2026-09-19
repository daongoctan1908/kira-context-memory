"""Bounded in-process admission control for authenticated product chat."""

import asyncio
from collections import deque
from collections.abc import Callable
from time import monotonic

from app.domain.errors.chat import (
    ChatConcurrencyLimitError,
    ChatRateLimitExceededError,
)


class ChatTrafficLease:
    """Idempotently release one active stream slot."""

    def __init__(self, guard: "ChatTrafficGuard", user_id: str) -> None:
        self._guard = guard
        self._user_id = user_id
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._guard._release(self._user_id)


class ChatTrafficGuard:
    """Per-process guard; multi-replica global limits require an external coordinator."""

    def __init__(
        self,
        *,
        requests_per_minute: int,
        max_concurrent_per_user: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            isinstance(requests_per_minute, bool)
            or not isinstance(requests_per_minute, int)
            or not 1 <= requests_per_minute <= 10_000
        ):
            raise ValueError("requests_per_minute must be between 1 and 10000")
        if (
            isinstance(max_concurrent_per_user, bool)
            or not isinstance(max_concurrent_per_user, int)
            or not 1 <= max_concurrent_per_user <= 100
        ):
            raise ValueError("max_concurrent_per_user must be between 1 and 100")
        self._rate = requests_per_minute
        self._concurrency = max_concurrent_per_user
        self._clock = clock
        self._requests: dict[str, deque[float]] = {}
        self._active: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._last_cleanup = 0.0

    async def acquire(self, user_id: str) -> ChatTrafficLease:
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        now = self._clock()
        async with self._lock:
            self._cleanup_stale_users(now)
            active = self._active.get(user_id, 0)
            if active >= self._concurrency:
                raise ChatConcurrencyLimitError
            requests = self._requests.setdefault(user_id, deque())
            cutoff = now - 60.0
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self._rate:
                raise ChatRateLimitExceededError
            requests.append(now)
            self._active[user_id] = active + 1
        return ChatTrafficLease(self, user_id)

    def _cleanup_stale_users(self, now: float) -> None:
        if now - self._last_cleanup < 60.0:
            return
        cutoff = now - 60.0
        for user_id, requests in tuple(self._requests.items()):
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if not requests and user_id not in self._active:
                self._requests.pop(user_id, None)
        self._last_cleanup = now

    async def _release(self, user_id: str) -> None:
        async with self._lock:
            active = self._active.get(user_id, 0)
            if active <= 1:
                self._active.pop(user_id, None)
            else:
                self._active[user_id] = active - 1
