"""Exercise the installed AsyncMemory pipeline with provider/storage doubles."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig
from mem0.memory.utils import parse_messages

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS
from app.application.services.memory_temporal import TEMPORAL_GUIDANCE
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import MemorySource
from app.infrastructure.memory.mem0_adapter import Mem0Adapter


async def test_per_call_prompt_reaches_native_extraction_but_not_embedding_or_saved_messages():
    store = MagicMock()
    store.get_formation_result.return_value = None
    store.search.return_value = []
    store.insert_with_formation_receipt.return_value = (True, [])
    embedder = MagicMock()
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    llm = MagicMock()
    llm.generate_response.return_value = '{"memory": []}'
    history = MagicMock()
    history.get_last_messages.return_value = [{"role": "user", "content": "old history"}]
    item = MemorySource(
        CompletedTurnReference("user-a", "session-a", uuid4(), "turn-a", 42),
        (
            ConversationMessage(
                "session-a",
                "turn-a",
                ConversationRole.USER,
                "Tuần này ưu tiên Hà Nội.",
                datetime(2026, 9, 14, 2, tzinfo=UTC),
            ),
            ConversationMessage(
                "session-a",
                "turn-a",
                ConversationRole.ASSISTANT,
                "Đã rõ.",
                datetime(2026, 9, 14, 2, 1, tzinfo=UTC),
            ),
        ),
        uuid4(),
    )
    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        memory = AsyncMemory(MemoryConfig(custom_instructions=MEMORY_EXTRACTION_INSTRUCTIONS))
        memory_adapter = Mem0Adapter(memory, search_timeout_seconds=2, operation_timeout_seconds=5)
        result = await memory_adapter.process_memory(item)
        assert not result.events
        assert memory.custom_instructions == MEMORY_EXTRACTION_INSTRUCTIONS
        raw = [
            {"role": message.role.value, "content": message.content} for message in item.messages
        ]
        embedder.embed.assert_called_once_with(parse_messages(raw), "search")
        history.save_messages.assert_called_once()
        assert history.save_messages.call_args.args[0] == raw
        prompt = llm.generate_response.call_args.kwargs["messages"][1]["content"]
        assert MEMORY_EXTRACTION_INSTRUCTIONS in prompt
        section = prompt.split(TEMPORAL_GUIDANCE, 1)[1].split("\n\n# Output:", 1)[0]
        table = json.loads(section)
        assert table == {"source_time": ["2026-09-14T09:00:00+07:00", "2026-09-14T09:01:00+07:00"]}
        assert "## Last k Messages\nuser: old history" in prompt
        assert store.insert_with_formation_receipt.call_args.kwargs["event_id"] == str(
            item.formation_event_id
        )
        # Replay after a committed empty extraction still bypasses providers.
        store.get_formation_result.return_value = []
        await memory_adapter.process_memory(item)
        llm.generate_response.assert_called_once()
        embedder.embed.assert_called_once()
