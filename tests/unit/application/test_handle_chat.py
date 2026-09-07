from collections.abc import AsyncIterator

from app.domain.models.chat import ChatCommand
from app.domain.models.kira import KiraAuthResult, KiraEventKind, KiraStreamEvent
from tests.support.context_fakes import PRINCIPAL, make_use_case


def event(text: str) -> KiraStreamEvent:
    return KiraStreamEvent(
        kind=KiraEventKind.TEXT,
        raw_data=f'{{"text":"{text}"}}',
        payload={"text": text},
        text_fragment=text,
    )


class FakeEventStream(AsyncIterator[KiraStreamEvent]):
    def __init__(self, events: list[KiraStreamEvent]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self) -> "FakeEventStream":
        return self

    async def __anext__(self) -> KiraStreamEvent:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class FakeKiraClient:
    def __init__(self, stream: FakeEventStream) -> None:
        self.stream = stream
        self.messages: list[str] = []

    async def authenticate(self) -> KiraAuthResult:
        return KiraAuthResult(token="unused")

    async def chat_stream(self, message: str) -> AsyncIterator[KiraStreamEvent]:
        self.messages.append(message)
        return self.stream


async def test_use_case_forwards_only_current_message_and_accumulates_final_text() -> None:
    source = FakeEventStream([event("Dạ "), event("đúng rồi")])
    client = FakeKiraClient(source)
    use_case = make_use_case(client)

    session = await use_case.execute(
        ChatCommand(session_id="session-1", message="question"),
        principal=PRINCIPAL,
    )
    received = [item async for item in session]

    assert client.messages == ["question"]
    assert received == [event("Dạ "), event("đúng rồi")]
    assert session.final_text == "Dạ đúng rồi"


async def test_session_close_propagates_to_downstream_iterator() -> None:
    source = FakeEventStream([event("unused")])
    session = await make_use_case(FakeKiraClient(source)).execute(
        ChatCommand(session_id="session-1", message="question"),
        principal=PRINCIPAL,
    )

    await session.aclose()

    assert source.closed
    assert [item async for item in session] == []
