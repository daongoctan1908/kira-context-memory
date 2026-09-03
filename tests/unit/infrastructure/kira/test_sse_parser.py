import json

import pytest

from app.domain.errors.kira import KiraMalformedSseError
from app.domain.models.kira import KiraEventKind
from app.infrastructure.kira.sse_parser import parse_sse_line


def _line(payload: object) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def test_parser_ignores_non_data_lines() -> None:
    assert parse_sse_line("") is None
    assert parse_sse_line(": keep-alive") is None
    assert parse_sse_line("event: message") is None


def test_parser_extracts_status_and_correlation_ids() -> None:
    payload = {
        "data": {
            "response": [
                {
                    "type": "status_response",
                    "content": {
                        "statusResponse": {
                            "id": "step-1",
                            "name": "Phân tích yêu cầu",
                            "status": "in_progress",
                        }
                    },
                }
            ],
            "requestId": "request-1",
            "messageId": "message-1",
        },
        "type": 5,
    }

    event = parse_sse_line(_line(payload))

    assert event is not None
    assert event.kind is KiraEventKind.STATUS
    assert event.text_fragment is None
    assert event.request_id == "request-1"
    assert event.message_id == "message-1"
    assert event.payload == payload


def test_parser_collects_text_fragments_in_frame_order() -> None:
    payload = {
        "data": {
            "response": [
                {"type": "text", "content": {"text": "Dạ "}},
                {"type": "text", "content": {"text": "đúng rồi"}},
            ],
            "requestId": "request-2",
        }
    }

    event = parse_sse_line(_line(payload))

    assert event is not None
    assert event.kind is KiraEventKind.TEXT
    assert event.text_fragment == "Dạ đúng rồi"


def test_parser_preserves_unknown_valid_frame() -> None:
    payload = {"data": {"response": [{"type": "future_type", "content": {"x": 1}}]}}
    line = _line(payload)

    event = parse_sse_line(line)

    assert event is not None
    assert event.kind is KiraEventKind.UNKNOWN
    assert event.raw_data == line.removeprefix("data: ")
    assert event.payload == payload


@pytest.mark.parametrize("line", ["data:", "data: not-json", "data: []", "data: null"])
def test_parser_rejects_malformed_data_frame(line: str) -> None:
    with pytest.raises(KiraMalformedSseError):
        parse_sse_line(line)
