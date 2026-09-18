"""Monotonic stage timing, retry accounting, percentiles, and trace fail-open behavior."""

from datetime import UTC, datetime

import pytest

from evaluation.artifacts import CaseAttemptArtifact
from evaluation.models import Outcome, Suite
from evaluation.scoring import output_sha256
from evaluation.timing import (
    TimingOutcome,
    TimingRecorder,
    TimingReport,
    TimingStage,
    nearest_rank,
)


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_mock_clock_locks_stage_boundaries_and_attempt_numbers():
    recorder = TimingRecorder(clock=_Clock(1.0, 1.125, 2.0, 2.05))

    with recorder.measure(TimingStage.RETRIEVAL, sdk_retry_count=1):
        pass
    with recorder.measure(TimingStage.RETRIEVAL):
        pass

    report = recorder.report()
    assert [(item.stage, item.attempt, item.duration_ms) for item in report.attempts] == [
        (TimingStage.RETRIEVAL, 1, 125.0),
        (TimingStage.RETRIEVAL, 2, pytest.approx(50.0)),
    ]
    summary = report.stages[0]
    assert summary.stage is TimingStage.RETRIEVAL
    assert summary.attempts == 2
    assert summary.successful_attempts == 2
    assert summary.sdk_retries == 1
    assert summary.worker_retries == 0


def test_timeout_and_error_attempts_are_counted_and_exceptions_propagate():
    recorder = TimingRecorder(clock=_Clock(1.0, 1.1, 2.0, 2.2))

    with pytest.raises(TimeoutError):
        with recorder.measure(TimingStage.MEMORY_READINESS, worker_retry_count=2):
            raise TimeoutError
    with pytest.raises(RuntimeError, match="business failure"):
        with recorder.measure(TimingStage.FORMATION):
            raise RuntimeError("business failure")

    summaries = {item.stage: item for item in recorder.report().stages}
    assert summaries[TimingStage.MEMORY_READINESS].timeout_attempts == 1
    assert summaries[TimingStage.MEMORY_READINESS].worker_retries == 2
    assert summaries[TimingStage.FORMATION].error_attempts == 1


def test_kira_ttft_and_completion_share_the_same_monotonic_start():
    recorder = TimingRecorder(clock=_Clock(10.0, 10.05, 10.2))
    stream = recorder.start_kira(sdk_retry_count=1)

    ttft = stream.first_token()
    completion = stream.finish()[0]

    assert ttft.stage is TimingStage.KIRA_TTFT
    assert ttft.duration_ms == pytest.approx(50)
    assert completion.stage is TimingStage.KIRA_COMPLETION
    assert completion.duration_ms == pytest.approx(200)
    assert ttft.sdk_retry_count == completion.sdk_retry_count == 1


def test_kira_failure_before_first_token_records_both_failed_stages():
    recorder = TimingRecorder(clock=_Clock(3.0, 3.4, 3.4))
    stream = recorder.start_kira()

    attempts = stream.finish(TimingOutcome.TIMEOUT)

    assert [item.stage for item in attempts] == [
        TimingStage.KIRA_TTFT,
        TimingStage.KIRA_COMPLETION,
    ]
    assert all(item.outcome is TimingOutcome.TIMEOUT for item in attempts)


def test_percentiles_are_deterministic_nearest_rank():
    values = (40.0, 10.0, 30.0, 20.0)

    assert nearest_rank(values, 50) == 20
    assert nearest_rank(values, 95) == 40
    assert nearest_rank(values, 99) == 40
    with pytest.raises(ValueError, match="must not be empty"):
        nearest_rank((), 50)
    with pytest.raises(ValueError, match="between zero and 100"):
        nearest_rank(values, 0)


def test_trace_lookup_failure_and_invalid_trace_id_fail_open():
    def unavailable_trace_backend() -> str:
        raise ConnectionError("telemetry backend unavailable")

    recorder = TimingRecorder(
        clock=_Clock(1.0, 1.1, 2.0, 2.1),
        trace_id_supplier=unavailable_trace_backend,
    )
    with recorder.measure(TimingStage.REWRITE):
        pass
    with recorder.measure(TimingStage.RETRIEVAL, trace_id="not-an-otel-trace"):
        pass

    assert [item.trace_id for item in recorder.report().attempts] == [None, None]


def test_valid_trace_is_only_an_optional_debug_reference():
    trace_id = "a" * 32
    recorder = TimingRecorder(clock=_Clock(1.0, 1.01))
    with recorder.measure(TimingStage.REWRITE, trace_id=trace_id):
        pass

    report = recorder.report()
    assert report.attempts[0].trace_id == trace_id
    assert "score" not in report.model_dump(mode="json")
    assert "outcome" not in report.model_dump(mode="json")


def test_timing_report_round_trips_inside_case_artifact_without_changing_quality():
    recorder = TimingRecorder(clock=_Clock(1.0, 1.1))
    with recorder.measure(TimingStage.REWRITE):
        pass
    output = {"rewrite": "FTTH Hà Nội tháng 08/2026?"}
    artifact = CaseAttemptArtifact(
        case_id="conv01:rewrite:q1",
        suite=Suite.REWRITE,
        attempt=1,
        completed_at=datetime(2026, 9, 18, tzinfo=UTC),
        outcome=Outcome.FAIL,
        output=output,
        output_sha256=output_sha256(output),
        reason_codes=("semantic_mismatch",),
        timing=recorder.report(),
    )

    restored = CaseAttemptArtifact.model_validate_json(artifact.model_dump_json())

    assert restored.outcome is Outcome.FAIL
    assert restored.timing == recorder.report()
    assert restored.timing is not None
    assert restored.timing.clock == "monotonic"


def test_report_rejects_summaries_that_do_not_cover_attempts():
    recorder = TimingRecorder(clock=_Clock(1.0, 1.1))
    with recorder.measure(TimingStage.QUEUE_WAIT):
        pass
    report = recorder.report()

    with pytest.raises(ValueError, match="do not cover"):
        TimingReport(attempts=report.attempts, stages=())


def test_timers_cannot_finish_twice_and_retry_counts_are_typed():
    recorder = TimingRecorder(clock=_Clock(1.0, 1.1))
    timer = recorder.start(TimingStage.FORMATION)
    timer.finish()
    with pytest.raises(RuntimeError, match="already finished"):
        timer.finish()

    with pytest.raises(ValueError, match="retry counts"):
        TimingRecorder().start(TimingStage.FORMATION, sdk_retry_count=True)
