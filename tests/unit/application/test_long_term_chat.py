import asyncio

import pytest

from app.domain.errors.conversation import ConversationStoreConnectionError
from app.domain.errors.memory import (
    LongTermMemoryConfigurationError,
    LongTermMemoryConnectionError,
    LongTermMemoryOperationError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.errors.query_rewriter import QueryRewriterTimeoutError
from app.domain.models.chat import ChatCommand
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.memory import LongTermMemory
from app.infrastructure.observability.context import ContextTelemetry
from tests.integration.test_gateway_api import FakeKiraClient
from tests.support.context_fakes import FakeRewriter, MemoryStore, make_use_case, pair

COMMAND = ChatCommand("session-1", "Hưng Yên thì sao?")
PRINCIPAL = AuthenticatedPrincipal("trusted-user")


def memory(index: int) -> LongTermMemory:
    return LongTermMemory(
        memory_id=f"memory-{index}",
        content=f"ranked memory {index}",
        score=1 - index / 100,
        metadata={"owner": "provider-private"},
    )


class FakeLongTermMemory:
    def __init__(self, memories=(), *, error=None, on_search=None):
        self.memories = tuple(memories)
        self.error = error
        self.on_search = on_search
        self.searches = []

    async def search(self, user_id, query, *, top_k, threshold):
        self.searches.append((user_id, query, top_k, threshold))
        if self.on_search is not None:
            await self.on_search()
        if self.error is not None:
            raise self.error
        return self.memories


async def test_combines_ranked_ltm_with_recent_using_trusted_user_and_original_query():
    memories = (memory(1), memory(2))
    recent = pair()
    ltm = FakeLongTermMemory(memories)
    rewriter = FakeRewriter("rewritten with memory")
    client = FakeKiraClient()

    telemetry = ContextTelemetry()
    await make_use_case(
        client,
        conversation_store=MemoryStore(recent),
        query_rewriter=rewriter,
        long_term_memory=ltm,
        memory_search_top_k=7,
        memory_search_threshold=0.35,
        observer=telemetry,
    ).execute(COMMAND, principal=PRINCIPAL)

    assert ltm.searches == [(PRINCIPAL.user_id, COMMAND.message, 7, 0.35)]
    assert client.messages == ["rewritten with memory"]
    assert rewriter.contexts[0].recent_messages == recent
    assert rewriter.contexts[0].long_term_memories == memories
    assert rewriter.contexts[0].current_query == COMMAND.message
    assert (
        telemetry.registry.get_sample_value(
            "kira_memory_search_total",
            {"outcome": "success"},
        )
        == 1
    )
    assert telemetry.registry.get_sample_value("kira_memory_search_results_sum") == 2


async def test_ltm_only_context_still_invokes_rewriter_for_cross_session_recall():
    ltm = FakeLongTermMemory((memory(1),))
    rewriter = FakeRewriter("standalone from cross-session memory")
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=MemoryStore(),
        query_rewriter=rewriter,
        long_term_memory=ltm,
    ).execute(COMMAND, principal=PRINCIPAL)

    assert rewriter.contexts[0].recent_messages == ()
    assert rewriter.contexts[0].long_term_memories == (memory(1),)
    assert client.messages == ["standalone from cross-session memory"]


async def test_empty_recent_and_ltm_bypasses_rewriter():
    ltm = FakeLongTermMemory()
    rewriter = FakeRewriter(error=QueryRewriterTimeoutError())
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=MemoryStore(),
        query_rewriter=rewriter,
        long_term_memory=ltm,
    ).execute(COMMAND, principal=PRINCIPAL)

    assert len(ltm.searches) == 1
    assert rewriter.contexts == []
    assert client.messages == [COMMAND.message]


@pytest.mark.parametrize(
    "error",
    [
        LongTermMemoryConfigurationError(),
        LongTermMemoryConnectionError(),
        LongTermMemoryOperationError(),
        LongTermMemoryProtocolError(),
        LongTermMemoryTimeoutError(),
    ],
)
async def test_memory_failure_degrades_to_recent_and_current(error):
    recent = pair()
    rewriter = FakeRewriter("rewritten from recent")
    client = FakeKiraClient()
    telemetry = ContextTelemetry()

    await make_use_case(
        client,
        conversation_store=MemoryStore(recent),
        query_rewriter=rewriter,
        long_term_memory=FakeLongTermMemory(error=error),
        observer=telemetry,
    ).execute(COMMAND, principal=PRINCIPAL)

    assert rewriter.contexts[0].recent_messages == recent
    assert rewriter.contexts[0].long_term_memories == ()
    assert client.messages == ["rewritten from recent"]
    assert (
        telemetry.registry.get_sample_value(
            "kira_context_degraded_total",
            {"dependency": "mem0", "operation": "memory_search"},
        )
        == 1
    )


async def test_use_case_enforces_memory_search_deadline_and_uses_recent():
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    rewriter = FakeRewriter("rewritten after memory timeout")
    client = FakeKiraClient()
    await asyncio.wait_for(
        make_use_case(
            client,
            conversation_store=MemoryStore(pair()),
            query_rewriter=rewriter,
            long_term_memory=FakeLongTermMemory(on_search=hang),
            memory_search_timeout_seconds=0.01,
        ).execute(COMMAND, principal=PRINCIPAL),
        timeout=1,
    )

    assert started.is_set()
    assert rewriter.contexts[0].long_term_memories == ()
    assert client.messages == ["rewritten after memory timeout"]


async def test_rewriter_failure_with_ltm_only_still_uses_original_query():
    rewriter = FakeRewriter(error=QueryRewriterTimeoutError())
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=MemoryStore(),
        query_rewriter=rewriter,
        long_term_memory=FakeLongTermMemory((memory(1),)),
    ).execute(COMMAND, principal=PRINCIPAL)

    assert len(rewriter.contexts) == 1
    assert rewriter.contexts[0].long_term_memories == (memory(1),)
    assert client.messages == [COMMAND.message]


async def test_postgres_failure_discards_successful_ltm_and_uses_current_only():
    ltm = FakeLongTermMemory((memory(1),))
    rewriter = FakeRewriter("must not be used")
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=MemoryStore(read_error=ConversationStoreConnectionError()),
        query_rewriter=rewriter,
        long_term_memory=ltm,
    ).execute(COMMAND, principal=PRINCIPAL)

    assert len(ltm.searches) == 1
    assert rewriter.contexts == []
    assert client.messages == [COMMAND.message]


async def test_postgres_and_memory_failure_together_use_current_only():
    rewriter = FakeRewriter("must not be used")
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=MemoryStore(read_error=ConversationStoreConnectionError()),
        query_rewriter=rewriter,
        long_term_memory=FakeLongTermMemory(error=LongTermMemoryConnectionError()),
    ).execute(COMMAND, principal=PRINCIPAL)

    assert rewriter.contexts == []
    assert client.messages == [COMMAND.message]


async def test_postgres_timeout_discards_successful_ltm():
    started = asyncio.Event()

    async def hang(user_id, session_id, limit):
        started.set()
        await asyncio.Event().wait()

    store = MemoryStore()
    store.read_recent = hang
    rewriter = FakeRewriter("must not be used")
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=store,
        query_rewriter=rewriter,
        long_term_memory=FakeLongTermMemory((memory(1),)),
        store_timeout_seconds=0.01,
    ).execute(COMMAND, principal=PRINCIPAL)

    assert started.is_set()
    assert rewriter.contexts == []
    assert client.messages == [COMMAND.message]


async def test_postgres_and_memory_reads_start_concurrently():
    store_started = asyncio.Event()
    memory_started = asyncio.Event()
    store = MemoryStore()

    async def read_recent(user_id, session_id, limit):
        store_started.set()
        await memory_started.wait()
        return pair()

    async def search_started():
        memory_started.set()
        await store_started.wait()

    store.read_recent = read_recent
    client = FakeKiraClient()
    await asyncio.wait_for(
        make_use_case(
            client,
            conversation_store=store,
            long_term_memory=FakeLongTermMemory((memory(1),), on_search=search_started),
        ).execute(COMMAND, principal=PRINCIPAL),
        timeout=1,
    )

    assert store_started.is_set()
    assert memory_started.is_set()
    assert client.messages == ["standalone query"]


async def test_unexpected_memory_error_is_not_silently_degraded():
    client = FakeKiraClient()
    with pytest.raises(RuntimeError, match="programming failure"):
        await make_use_case(
            client,
            conversation_store=MemoryStore(pair()),
            long_term_memory=FakeLongTermMemory(error=RuntimeError("programming failure")),
        ).execute(COMMAND, principal=PRINCIPAL)

    assert client.messages == []


async def test_unexpected_memory_error_cancels_parallel_postgres_read():
    store = MemoryStore()
    store_started = asyncio.Event()
    store_cancelled = asyncio.Event()

    async def hanging_read(user_id, session_id, limit):
        store_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            store_cancelled.set()

    async def fail_after_store_starts():
        await store_started.wait()
        raise RuntimeError("programming failure")

    store.read_recent = hanging_read
    with pytest.raises(RuntimeError, match="programming failure"):
        await asyncio.wait_for(
            make_use_case(
                FakeKiraClient(),
                conversation_store=store,
                long_term_memory=FakeLongTermMemory(on_search=fail_after_store_starts),
            ).execute(COMMAND, principal=PRINCIPAL),
            timeout=1,
        )

    assert store_cancelled.is_set()


async def test_cancellation_during_memory_search_propagates_before_kira():
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    client = FakeKiraClient()
    task = asyncio.create_task(
        make_use_case(
            client,
            long_term_memory=FakeLongTermMemory(on_search=hang),
        ).execute(COMMAND, principal=PRINCIPAL)
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.messages == []


async def test_missing_identity_never_searches_ltm():
    ltm = FakeLongTermMemory((memory(1),))
    client = FakeKiraClient()
    telemetry = ContextTelemetry()

    await make_use_case(client, long_term_memory=ltm, observer=telemetry).execute(
        COMMAND, principal=None
    )

    assert ltm.searches == []
    assert client.messages == [COMMAND.message]
    assert (
        telemetry.registry.get_sample_value(
            "kira_memory_search_total",
            {"outcome": "bypass"},
        )
        == 1
    )


async def test_provider_contract_violation_discards_ltm_without_losing_recent():
    ltm = FakeLongTermMemory()
    ltm.memories = ("not-a-memory",)
    rewriter = FakeRewriter("recent-only")
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=MemoryStore(pair()),
        query_rewriter=rewriter,
        long_term_memory=ltm,
    ).execute(COMMAND, principal=PRINCIPAL)

    assert rewriter.contexts[0].long_term_memories == ()
    assert client.messages == ["recent-only"]


async def test_context_invariant_failure_fails_closed_to_current_only():
    class RejectingContextBuilder:
        def build(self, recent, current, memories):
            raise ValueError("invalid contextual input")

    rewriter = FakeRewriter("must not be used")
    client = FakeKiraClient()

    await make_use_case(
        client,
        conversation_store=MemoryStore(pair()),
        query_rewriter=rewriter,
        context_builder=RejectingContextBuilder(),
        long_term_memory=FakeLongTermMemory((memory(1),)),
    ).execute(COMMAND, principal=PRINCIPAL)

    assert rewriter.contexts == []
    assert client.messages == [COMMAND.message]


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_search_top_k": 0},
        {"memory_search_top_k": 11},
        {"memory_search_top_k": True},
        {"memory_search_threshold": -0.1},
        {"memory_search_threshold": 1.1},
        {"memory_search_threshold": True},
        {"memory_search_timeout_seconds": 0},
        {"memory_search_timeout_seconds": True},
        {"memory_formation_enabled": 1},
    ],
)
def test_rejects_invalid_memory_configuration(overrides):
    with pytest.raises(ValueError):
        make_use_case(FakeKiraClient(), **overrides)
