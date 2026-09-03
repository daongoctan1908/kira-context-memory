import asyncio
from collections.abc import Callable

from app.domain.models.kira import KiraAuthResult
from app.infrastructure.kira.token_manager import KiraTokenManager


class FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now


def authenticator(*results: KiraAuthResult) -> tuple[Callable[[], object], list[int]]:
    calls: list[int] = []
    remaining = iter(results)

    async def authenticate() -> KiraAuthResult:
        calls.append(1)
        return next(remaining)

    return authenticate, calls


async def test_token_is_reused_until_ttl_minus_skew() -> None:
    clock = FakeClock()
    authenticate, calls = authenticator(
        KiraAuthResult(token="token-1", token_expiration_time=100),
        KiraAuthResult(token="token-2", token_expiration_time=100),
    )
    manager = KiraTokenManager(authenticate, expiry_skew_seconds=10, clock=clock)

    assert await manager.get_token() == "token-1"
    clock.now = 189.9
    assert await manager.get_token() == "token-1"
    clock.now = 190.0
    assert await manager.get_token() == "token-2"
    assert len(calls) == 2


async def test_missing_or_short_ttl_disables_reuse() -> None:
    clock = FakeClock()
    authenticate, calls = authenticator(
        KiraAuthResult(token="token-1", token_expiration_time=None),
        KiraAuthResult(token="token-2", token_expiration_time=10),
        KiraAuthResult(token="token-3", token_expiration_time=100),
    )
    manager = KiraTokenManager(authenticate, expiry_skew_seconds=10, clock=clock)

    assert await manager.get_token() == "token-1"
    assert await manager.get_token() == "token-2"
    assert await manager.get_token() == "token-3"
    assert len(calls) == 3


async def test_invalidate_only_clears_expected_token() -> None:
    clock = FakeClock()
    authenticate, calls = authenticator(
        KiraAuthResult(token="token-1", token_expiration_time=100),
        KiraAuthResult(token="token-2", token_expiration_time=100),
    )
    manager = KiraTokenManager(authenticate, expiry_skew_seconds=10, clock=clock)

    assert await manager.get_token() == "token-1"
    await manager.invalidate(expected_token="different-token")
    assert await manager.get_token() == "token-1"
    await manager.invalidate(expected_token="token-1")
    assert await manager.get_token() == "token-2"
    assert len(calls) == 2


async def test_concurrent_callers_share_one_authentication() -> None:
    clock = FakeClock()
    authentication_started = asyncio.Event()
    release_authentication = asyncio.Event()
    calls = 0

    async def authenticate() -> KiraAuthResult:
        nonlocal calls
        calls += 1
        authentication_started.set()
        await release_authentication.wait()
        return KiraAuthResult(token="shared-token", token_expiration_time=100)

    manager = KiraTokenManager(authenticate, expiry_skew_seconds=10, clock=clock)
    tasks = [asyncio.create_task(manager.get_token()) for _ in range(5)]

    await authentication_started.wait()
    release_authentication.set()
    tokens = await asyncio.gather(*tasks)

    assert tokens == ["shared-token"] * 5
    assert calls == 1
