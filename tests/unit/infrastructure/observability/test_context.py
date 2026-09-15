import asyncio

from app.infrastructure.observability.context import (
    bind_observability_context,
    current_context_fields,
)


async def test_context_is_isolated_between_concurrent_tasks_and_reset_after_use() -> None:
    async def capture(correlation_id: str, turn_id: str) -> dict[str, str]:
        with bind_observability_context(correlation_id=correlation_id, turn_id=turn_id):
            await asyncio.sleep(0)
            return current_context_fields()

    first, second = await asyncio.gather(capture("corr-a", "turn-a"), capture("corr-b", "turn-b"))

    assert first == {"correlation_id": "corr-a", "turn_id": "turn-a"}
    assert second == {"correlation_id": "corr-b", "turn_id": "turn-b"}
    assert current_context_fields() == {}


def test_nested_context_restores_independent_identifiers() -> None:
    with bind_observability_context(
        correlation_id="correlation-outer",
        turn_id="turn-outer",
        event_id="event-outer",
    ):
        with bind_observability_context(correlation_id="correlation-inner"):
            assert current_context_fields() == {
                "correlation_id": "correlation-inner",
                "turn_id": "turn-outer",
                "event_id": "event-outer",
            }
        assert current_context_fields() == {
            "correlation_id": "correlation-outer",
            "turn_id": "turn-outer",
            "event_id": "event-outer",
        }

    assert current_context_fields() == {}
