from app.infrastructure.observability.redaction import masked_json, redact_text, safe_log_value


class DangerousValue:
    def __str__(self) -> str:
        raise RuntimeError("must not stringify")


def test_redaction_masks_credentials_email_and_phone() -> None:
    result = redact_text(
        "password=hunter2 api_key:abc Authorization: Bearer token-value "
        "user@example.com phone +84 912 345 678"
    )

    assert "hunter2" not in result
    assert "abc" not in result
    assert "token-value" not in result
    assert "user@example.com" not in result
    assert "+84 912 345 678" not in result
    assert result.count("[REDACTED") >= 4


def test_redaction_failure_and_oversize_payload_fail_closed() -> None:
    assert safe_log_value(DangerousValue()) is None
    assert redact_text(DangerousValue()) == "[REDACTED]"
    assert masked_json({"safe": "x" * 5000}, max_length=32) == '"[REDACTED_OVERSIZE]"'
    assert masked_json({DangerousValue(): "secret"}) == "{}"
