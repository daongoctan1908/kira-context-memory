import json
from pathlib import Path
from uuid import uuid4

import pytest

from scripts.smoke_week4_async import Week4SmokeError
from scripts.smoke_week4_retry import (
    EXPECTED_ERROR_CLASS,
    JobEvidence,
    _require_completed_job,
    _require_dead_listing,
    parse_args,
)


def job_evidence(**overrides: object) -> JobEvidence:
    values = {
        "event_id": uuid4(),
        "status": "dead",
        "attempt_count": 5,
        "requeue_count": 0,
        "last_error_class": EXPECTED_ERROR_CLASS,
        "lifecycle_event_count": None,
    }
    values.update(overrides)
    return JobEvidence(**values)  # type: ignore[arg-type]


def test_completed_job_requires_one_lifecycle_event_and_no_error() -> None:
    _require_completed_job(
        job_evidence(
            status="completed",
            attempt_count=2,
            last_error_class=None,
            lifecycle_event_count=1,
        )
    )

    with pytest.raises(Week4SmokeError):
        _require_completed_job(job_evidence(status="completed", lifecycle_event_count=0))
    with pytest.raises(Week4SmokeError):
        _require_completed_job(
            job_evidence(
                status="completed",
                last_error_class=EXPECTED_ERROR_CLASS,
                lifecycle_event_count=1,
            )
        )


def test_dead_listing_matches_exact_sanitized_operator_projection() -> None:
    job = job_evidence()
    payload = {
        "count": 1,
        "items": [
            {
                "attempt_count": 5,
                "created_at": "2026-09-10T00:00:00Z",
                "dead_at": "2026-09-10T00:00:05Z",
                "event_id": str(job.event_id),
                "last_error_class": EXPECTED_ERROR_CLASS,
                "requeue_count": 0,
            }
        ],
        "limit": 1000,
    }

    _require_dead_listing(payload, job, forbidden=("private-message",))

    contaminated = json.loads(json.dumps(payload))
    contaminated["private"] = "private-message"
    with pytest.raises(Week4SmokeError):
        _require_dead_listing(contaminated, job, forbidden=("private-message",))


@pytest.mark.parametrize(
    "field",
    ["attempt_count", "requeue_count", "last_error_class"],
)
def test_dead_listing_rejects_incorrect_job_state(field: str) -> None:
    job = job_evidence()
    item = {
        "attempt_count": 5,
        "event_id": str(job.event_id),
        "last_error_class": EXPECTED_ERROR_CLASS,
        "requeue_count": 0,
    }
    item[field] = "wrong"

    with pytest.raises(Week4SmokeError):
        _require_dead_listing({"items": [item]}, job, forbidden=())


def test_parse_args_normalizes_urls_and_resolves_compose_file(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    options = parse_args(
        [
            "--gateway-url",
            "http://gateway.test/",
            "--worker-url",
            "http://worker.test/",
            "--mock-memory-llm-url",
            "http://memory.test/",
            "--compose-file",
            str(compose_file),
            "--timeout",
            "3.5",
        ]
    )

    assert options.gateway_url == "http://gateway.test"
    assert options.worker_url == "http://worker.test"
    assert options.mock_memory_llm_url == "http://memory.test"
    assert options.compose_file == compose_file.resolve()
    assert options.timeout_seconds == 3.5


def test_parse_args_rejects_missing_compose_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--compose-file", str(tmp_path / "missing.yaml")])
