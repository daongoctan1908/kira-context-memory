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


def test_telecom_identifiers_network_values_and_credentials_are_masked() -> None:
    result = redact_text(
        "IMSI=452011234567890 ICCID=8984041234567890123 national_id=012345678901 "
        "subscriber_id=CUST-009 contract_id=CTR-31 payment_id=PAY-2 "
        "ip 10.20.30.40 mac aa:bb:cc:dd:ee:ff Bearer opaque-token-value "
        "sk-abcdefghijklmnop postgresql://kira:private@db.internal/kira "
        "CONFIDENTIAL=site-secret"
    )

    for forbidden in (
        "452011234567890",
        "8984041234567890123",
        "012345678901",
        "CUST-009",
        "CTR-31",
        "PAY-2",
        "10.20.30.40",
        "aa:bb:cc:dd:ee:ff",
        "opaque-token-value",
        "sk-abcdefghijklmnop",
        "private",
        "site-secret",
    ):
        assert forbidden not in result


def test_domain_evidence_is_not_overmasked() -> None:
    text = (
        "Retention KPI = active_end / active_start * 100%; threshold 98%; 2026-09-15; site HNI-001"
    )

    assert redact_text(text) == text
