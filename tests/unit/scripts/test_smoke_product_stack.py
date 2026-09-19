import pytest

from scripts.smoke_product_stack import (
    ProductSmokeError,
    _parse_product_events,
    parse_args,
)


def test_parse_product_events_preserves_named_event_order() -> None:
    events = _parse_product_events(
        'event: message.started\ndata: {"client_message_id":"one"}\n\n'
        'event: message.delta\ndata: {"text":"hello"}\n\n'
        'event: message.completed\ndata: {"replayed":false}\n\n'
    )

    assert [name for name, _ in events] == [
        "message.started",
        "message.delta",
        "message.completed",
    ]
    assert events[1][1] == {"text": "hello"}


def test_parse_product_events_rejects_invalid_json_without_echoing_content() -> None:
    with pytest.raises(ProductSmokeError):
        _parse_product_events("event: message.delta\ndata: private-invalid-json\n\n")


def test_parse_args_reads_product_endpoints_and_credentials(monkeypatch) -> None:
    monkeypatch.setenv("PRODUCT_ADMIN_USERNAME", "operator")
    monkeypatch.setenv("PRODUCT_ADMIN_PASSWORD", "local-password-value")
    monkeypatch.setenv("PRODUCT_DATABASE_URL", "postgresql+asyncpg://local/test")

    options = parse_args(["--frontend-url", "http://localhost:9000/", "--timeout", "5"])

    assert options.frontend_url == "http://localhost:9000"
    assert options.username == "operator"
    assert options.password == "local-password-value"
    assert options.database_url == "postgresql+asyncpg://local/test"
    assert options.timeout_seconds == 5


def test_parse_args_rejects_non_positive_timeout() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--timeout", "0"])
