import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.application.services.context_builder import ContextBuilder
from app.application.use_cases.handle_chat import ChatStreamSession
from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.errors.kira import KiraTimeoutError
from app.domain.errors.query_rewriter import (
    QueryRewriterConnectionError,
    QueryRewriterHttpError,
    QueryRewriterProtocolError,
    QueryRewriterTimeoutError,
)
from app.domain.models.chat import ChatCommand
from app.infrastructure.observability.context import ContextTelemetry
from tests.integration.test_gateway_api import FakeKiraClient, ListStream, kira_event
from tests.support.context_fakes import PRINCIPAL, FakeRewriter, MemoryStore, make_use_case, pair

COMMAND = ChatCommand("session-1", "Hưng Yên thì sao?")


def answer(text=" exact\nanswer "):
    return kira_event('{"text":"test"}', text)


async def drain(session):
    return [event async for event in session]


async def test_rewrite_and_persist_original_with_exact_assistant_once():
    store = MemoryStore(pair())
    rewriter = FakeRewriter("Doanh thu Hưng Yên tháng 8?")
    client = FakeKiraClient(events=[answer(" exact\n"), answer("answer ")])
    telemetry = ContextTelemetry()
    session = await make_use_case(
        client,
        conversation_store=store,
        query_rewriter=rewriter,
        observer=telemetry,
    ).execute(COMMAND, principal=PRINCIPAL, correlation_id="correlation-test")
    assert store.writes == []
    assert client.messages == [rewriter.output]
    assert store.reads == [("session-1", 10)]
    await drain(session)
    await drain(session)
    await session.aclose()
    assert len(store.writes) == 1
    user, assistant = store.writes[0]
    assert user.content == COMMAND.message
    assert assistant.content == " exact\nanswer "
    assert user.turn_id == assistant.turn_id != "old-turn"
    assert UUID(user.turn_id).version == 4
    assert user.timestamp <= assistant.timestamp
    assert user.session_id == assistant.session_id == COMMAND.session_id
    assert rewriter.contexts[0].current_query == COMMAND.message
    assert (
        telemetry.registry.get_sample_value(
            "kira_conversation_write_total",
            {"outcome": "inserted"},
        )
        == 1
    )


@pytest.mark.parametrize("budget,history", [(3000, ()), (1, pair())])
async def test_empty_or_fully_trimmed_context_bypasses_rewriter(budget, history):
    rewriter = FakeRewriter(error=QueryRewriterTimeoutError())
    client = FakeKiraClient()
    await make_use_case(
        client,
        conversation_store=MemoryStore(history),
        query_rewriter=rewriter,
        context_builder=ContextBuilder(recent_token_budget=budget),
    ).execute(COMMAND, principal=PRINCIPAL)
    assert client.messages == [COMMAND.message]
    assert rewriter.contexts == []


@pytest.mark.parametrize(
    "error",
    [
        ConversationStoreConfigurationError(),
        ConversationStoreConnectionError(),
        ConversationStoreOperationError(),
        ConversationStoreProtocolError(),
        TimeoutError(),
    ],
)
async def test_read_failure_bypasses_even_failing_rewriter(error):
    telemetry = ContextTelemetry()
    rewriter = FakeRewriter(error=QueryRewriterTimeoutError())
    client = FakeKiraClient(events=[answer()])
    store = MemoryStore(read_error=error)
    session = await make_use_case(
        client,
        conversation_store=store,
        query_rewriter=rewriter,
        observer=telemetry,
    ).execute(COMMAND, principal=PRINCIPAL)
    await drain(session)
    assert client.messages == [COMMAND.message]
    assert rewriter.contexts == []
    assert len(store.writes) == 1
    assert (
        telemetry.registry.get_sample_value(
            "kira_context_degraded_total",
            {"dependency": "postgresql", "operation": "postgres_read"},
        )
        == 1
    )


@pytest.mark.parametrize(
    "error",
    [
        QueryRewriterTimeoutError(),
        QueryRewriterProtocolError(),
        QueryRewriterConnectionError(),
        QueryRewriterHttpError(status_code=503),
    ],
)
async def test_rewriter_failure_uses_original(error):
    client = FakeKiraClient()
    telemetry = ContextTelemetry()
    await make_use_case(
        client,
        conversation_store=MemoryStore(pair()),
        query_rewriter=FakeRewriter(error=error),
        observer=telemetry,
    ).execute(COMMAND, principal=PRINCIPAL)
    assert client.messages == [COMMAND.message]
    assert (
        telemetry.registry.get_sample_value(
            "kira_context_rewrite_total",
            {"outcome": "error"},
        )
        == 1
    )


async def test_wrong_session_from_store_is_never_sent_to_rewriter():
    client = FakeKiraClient()
    rewriter = FakeRewriter()
    await make_use_case(
        client,
        conversation_store=MemoryStore(pair("other-session")),
        query_rewriter=rewriter,
    ).execute(COMMAND, principal=PRINCIPAL)
    assert client.messages == [COMMAND.message]
    assert rewriter.contexts == []


@pytest.mark.parametrize("failure", ["before", "midstream", "close", "empty", "whitespace"])
async def test_unsuccessful_or_textless_stream_does_not_persist(failure):
    error = KiraTimeoutError(stage="chat stream")
    store = MemoryStore()
    client = FakeKiraClient(
        events=[] if failure == "empty" else [answer(" \n" if failure == "whitespace" else "part")],
        open_error=error if failure == "before" else None,
        stream_error=error if failure == "midstream" else None,
    )
    use_case = make_use_case(client, conversation_store=store)
    if failure == "before":
        with pytest.raises(KiraTimeoutError):
            await use_case.execute(COMMAND, principal=PRINCIPAL)
    else:
        session = await use_case.execute(COMMAND, principal=PRINCIPAL)
        if failure == "midstream":
            with pytest.raises(KiraTimeoutError):
                await drain(session)
        elif failure == "close":
            await anext(session)
            await session.aclose()
            await drain(session)
        else:
            await drain(session)
        assert client.last_stream.closed
    assert store.writes == []


@pytest.mark.parametrize(
    "write_error", [ConversationStoreConnectionError(), RuntimeError("private")]
)
async def test_write_failure_is_only_observed_never_raised(write_error):
    telemetry = ContextTelemetry()
    session = await make_use_case(
        FakeKiraClient(events=[answer()]),
        conversation_store=MemoryStore(write_error=write_error),
        observer=telemetry,
    ).execute(COMMAND, principal=PRINCIPAL)
    assert len(await drain(session)) == 1
    assert (
        telemetry.registry.get_sample_value(
            "kira_conversation_write_total",
            {"outcome": "error"},
        )
        == 1
    )


async def test_duplicate_write_outcome():
    telemetry = ContextTelemetry()
    session = await make_use_case(
        FakeKiraClient(events=[answer()]),
        conversation_store=MemoryStore(inserted=False),
        observer=telemetry,
    ).execute(COMMAND, principal=PRINCIPAL)
    await drain(session)
    assert (
        telemetry.registry.get_sample_value(
            "kira_conversation_write_total",
            {"outcome": "duplicate"},
        )
        == 1
    )


async def test_missing_identity_uses_current_query_without_history_or_persistence():
    store = MemoryStore(pair())
    telemetry = ContextTelemetry()
    client = FakeKiraClient(events=[answer()])

    session = await make_use_case(
        client,
        conversation_store=store,
        observer=telemetry,
    ).execute(COMMAND, principal=None, correlation_id="anonymous-request")
    await drain(session)

    assert client.messages == [COMMAND.message]
    assert store.reads == []
    assert store.writes == []
    assert (
        telemetry.registry.get_sample_value(
            "kira_context_degraded_total",
            {"dependency": "identity", "operation": "identity"},
        )
        == 1
    )


@pytest.mark.parametrize("operation", ["read_recent", "append_turn"])
async def test_total_store_operation_deadline(operation):
    store = MemoryStore()
    started = asyncio.Event()

    async def hang(*args):
        started.set()
        await asyncio.Event().wait()

    setattr(store, operation, hang)
    telemetry = ContextTelemetry()
    client = FakeKiraClient(events=[answer()])
    async with asyncio.timeout(1):
        session = await make_use_case(
            client,
            conversation_store=store,
            store_timeout_seconds=0.01,
            observer=telemetry,
        ).execute(COMMAND, principal=PRINCIPAL)
        await drain(session)
    assert started.is_set()
    assert client.messages == [COMMAND.message]
    assert (
        telemetry.registry.get_sample_value(
            "kira_context_degraded_total",
            {
                "dependency": "postgresql",
                "operation": "postgres_read" if operation == "read_recent" else "postgres_write",
            },
        )
        == 1
    )


async def test_cancel_during_append_propagates_and_is_not_retried():
    store = MemoryStore()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def append(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    store.append_turn = append
    session = await make_use_case(
        FakeKiraClient(events=[answer()]),
        conversation_store=store,
    ).execute(COMMAND, principal=PRINCIPAL)
    task = asyncio.create_task(drain(session))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert await drain(session) == []
    assert store.writes == []


async def test_cancelled_source_is_closed_without_completion():
    started = asyncio.Event()

    async def source():
        started.set()
        await asyncio.Event().wait()
        yield answer()

    callback = AsyncMock()
    session = ChatStreamSession(source(), callback)
    task = asyncio.create_task(anext(session))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    callback.assert_not_awaited()


async def test_cleanup_cancel_can_be_retried_without_completion():
    source = ListStream([answer()])
    source.aclose = AsyncMock(side_effect=[asyncio.CancelledError(), None])
    callback = AsyncMock()
    session = ChatStreamSession(source, callback)
    with pytest.raises(asyncio.CancelledError):
        await session.aclose()
    await session.aclose()
    assert source.aclose.await_count == 2
    callback.assert_not_awaited()
