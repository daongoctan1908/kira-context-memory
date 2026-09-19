import pytest

from app.application.services.chat_traffic import ChatTrafficGuard
from app.domain.errors.chat import (
    ChatConcurrencyLimitError,
    ChatRateLimitExceededError,
)


async def test_concurrency_slot_is_held_until_idempotent_release() -> None:
    guard = ChatTrafficGuard(requests_per_minute=10, max_concurrent_per_user=1)
    lease = await guard.acquire("user-1")

    try:
        await guard.acquire("user-1")
    except ChatConcurrencyLimitError:
        pass
    else:
        raise AssertionError("concurrent request should be rejected")

    await lease.release()
    await lease.release()
    replacement = await guard.acquire("user-1")
    await replacement.release()


async def test_sliding_rate_limit_expires_without_counting_concurrency_rejections() -> None:
    current = 100.0
    guard = ChatTrafficGuard(
        requests_per_minute=2,
        max_concurrent_per_user=2,
        clock=lambda: current,
    )
    first = await guard.acquire("user-1")
    second = await guard.acquire("user-1")
    await first.release()
    await second.release()

    try:
        await guard.acquire("user-1")
    except ChatRateLimitExceededError:
        pass
    else:
        raise AssertionError("rate-limited request should be rejected")

    current += 61
    admitted = await guard.acquire("user-1")
    await admitted.release()


def test_guard_configuration_and_identity_are_validated() -> None:
    for rate, concurrency in ((0, 1), (True, 1), (1, 0), (1, True), ("1", 1)):
        try:
            ChatTrafficGuard(  # type: ignore[arg-type]
                requests_per_minute=rate,
                max_concurrent_per_user=concurrency,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid traffic guard configuration was accepted")


async def test_empty_user_identity_is_rejected() -> None:
    guard = ChatTrafficGuard(requests_per_minute=1, max_concurrent_per_user=1)
    with pytest.raises(ValueError, match="user_id"):
        await guard.acquire(" ")
