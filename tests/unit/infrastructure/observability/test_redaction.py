import json

import pytest

from app.infrastructure.observability.redaction import (
    masked_json,
    masked_json_snapshot,
    redact_text,
    safe_log_value,
)


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


def test_snapshot_with_lone_unicode_surrogate_omits_content_without_raising() -> None:
    assert masked_json_snapshot("\ud800") == (None, False, 0, "masking_error")


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


def test_json_credentials_embedded_in_prompt_are_masked_without_breaking_json() -> None:
    payload = {
        "password": 'hunter2 with "quotes"',
        "accessToken": "opaque token with spaces",
        "subscriber_id": "CUST-009",
        "imsi": 452011234567890,
        "message": "safe domain evidence",
    }

    result = json.loads(redact_text(json.dumps(payload)))

    assert result == {
        "password": "[REDACTED]",
        "accessToken": "[REDACTED]",
        "subscriber_id": "[REDACTED_ID]",
        "imsi": "[REDACTED_ID]",
        "message": "safe domain evidence",
    }


def test_sensitive_mapping_values_are_masked_regardless_of_type_or_nesting() -> None:
    payload = {
        "password": 1234,
        "credentials": {"username": "service", "value": "private-value"},
        "nested": [{"account-id": 73, "client_secret": ["private-secret"]}],
        "numbers": [452011234567890, 98, True],
    }

    assert json.loads(masked_json(payload)) == {
        "password": "[REDACTED]",
        "credentials": "[REDACTED]",
        "nested": [{"account-id": "[REDACTED_ID]", "client_secret": "[REDACTED]"}],
        "numbers": ["[REDACTED_ID]", 98, True],
    }


def test_structured_credentials_inside_prompt_are_masked_as_an_entire_field() -> None:
    payload = {"credentials": {"value": "private-secret", "username": "service"}, "hint": 98}

    result = redact_text("Payload: " + json.dumps(payload) + "\nKeep the KPI.")

    assert "private-secret" not in result
    assert "service" not in result
    assert json.loads(result.removeprefix("Payload: ").removesuffix("\nKeep the KPI.")) == {
        "credentials": "[REDACTED]",
        "hint": 98,
    }


@pytest.mark.parametrize(
    "content",
    ['{"password":"private secret with spaces', '{"password":"private secret\\'],
    ids=["missing-closing-quote", "cut-inside-json-escape"],
)
def test_unterminated_quoted_credential_is_masked_to_end_of_snapshot(content: str) -> None:
    result = redact_text(content)

    assert "private" not in result
    assert "with spaces" not in result


@pytest.mark.parametrize("prefix", ["", "secret="])
def test_private_key_prefix_without_end_marker_is_masked(prefix: str) -> None:
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "PRIVATEKEYBODY" * 500

    result = redact_text(prefix + key[:4096])

    assert "PRIVATEKEYBODY" not in result
    assert "PRIVATE KEY" not in result
    assert "[REDACTED" in result


@pytest.mark.parametrize(
    "content", ['"\\\n' * 2000, "tiếng Việt " * 2000], ids=["escaped-text", "unicode-text"]
)
def test_oversize_snapshot_is_valid_json_with_bounded_masked_preview(content: str) -> None:
    payload = {"password": "private-secret", "message": content}

    rendered, truncated, original_bytes, omitted = masked_json_snapshot(payload, max_length=128)

    assert rendered is not None and len(rendered) <= 128
    assert isinstance(json.loads(rendered), str)
    assert json.loads(rendered).endswith("[TRUNCATED]")
    assert "private-secret" not in rendered
    assert truncated is True
    assert original_bytes == len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    assert omitted is None
