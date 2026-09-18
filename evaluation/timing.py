"""Monotonic benchmark timing artifacts, independent of quality scoring and telemetry."""

import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, model_validator

from evaluation.models import EvalModel

_TRACE_ID = re.compile(r"^[a-f0-9]{32}$")


class TimingStage(StrEnum):
    FORMATION = "formation"
    RETRIEVAL = "retrieval"
    REWRITE = "rewrite"
    KIRA_TTFT = "kira_ttft"
    KIRA_COMPLETION = "kira_completion"
    QUEUE_WAIT = "queue_wait"
    MEMORY_READINESS = "memory_readiness"


class TimingOutcome(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"


class MonotonicClock(Protocol):
    def __call__(self) -> float: ...


class TimingAttempt(EvalModel):
    stage: TimingStage
    attempt: int = Field(ge=1, strict=True)
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    outcome: TimingOutcome
    sdk_retry_count: int = Field(default=0, ge=0, strict=True)
    worker_retry_count: int = Field(default=0, ge=0, strict=True)
    trace_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class StageTimingSummary(EvalModel):
    stage: TimingStage
    attempts: int = Field(ge=1, strict=True)
    successful_attempts: int = Field(ge=0, strict=True)
    timeout_attempts: int = Field(ge=0, strict=True)
    error_attempts: int = Field(ge=0, strict=True)
    sdk_retries: int = Field(ge=0, strict=True)
    worker_retries: int = Field(ge=0, strict=True)
    p50_ms: float = Field(ge=0, allow_inf_nan=False)
    p95_ms: float = Field(ge=0, allow_inf_nan=False)
    p99_ms: float = Field(ge=0, allow_inf_nan=False)
    percentile_method: Literal["nearest_rank"] = "nearest_rank"

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "StageTimingSummary":
        if self.successful_attempts + self.timeout_attempts + self.error_attempts != self.attempts:
            raise ValueError("timing outcome counts must add up to attempts")
        return self


class TimingReport(EvalModel):
    schema_version: Literal[1] = 1
    clock: Literal["monotonic"] = "monotonic"
    attempts: tuple[TimingAttempt, ...]
    stages: tuple[StageTimingSummary, ...]

    @model_validator(mode="after")
    def summaries_match_attempts(self) -> "TimingReport":
        attempt_stages = {attempt.stage for attempt in self.attempts}
        summary_stages = [summary.stage for summary in self.stages]
        if len(summary_stages) != len(set(summary_stages)):
            raise ValueError("timing report stage summaries must be unique")
        if attempt_stages != set(summary_stages):
            raise ValueError("timing report summaries do not cover all attempts")
        return self


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    """Return the deterministic nearest-rank percentile for a non-empty sample."""

    if not values:
        raise ValueError("percentile sample must not be empty")
    if not 0 < percentile <= 100 or not math.isfinite(percentile):
        raise ValueError("percentile must be finite and between zero and 100")
    if any(value < 0 or not math.isfinite(value) for value in values):
        raise ValueError("percentile samples must be finite and nonnegative")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


class StageTimer:
    def __init__(
        self,
        recorder: "TimingRecorder",
        stage: TimingStage,
        *,
        sdk_retry_count: int,
        worker_retry_count: int,
        trace_id: str | None,
    ) -> None:
        self._recorder = recorder
        self._stage = stage
        self._sdk_retry_count = sdk_retry_count
        self._worker_retry_count = worker_retry_count
        self._trace_id = trace_id
        self._started = recorder.clock()
        self._finished = False

    def finish(self, outcome: TimingOutcome = TimingOutcome.SUCCESS) -> TimingAttempt:
        if self._finished:
            raise RuntimeError("timing stage already finished")
        self._finished = True
        duration_ms = max(0.0, (self._recorder.clock() - self._started) * 1000)
        return self._recorder.record(
            stage=self._stage,
            duration_ms=duration_ms,
            outcome=outcome,
            sdk_retry_count=self._sdk_retry_count,
            worker_retry_count=self._worker_retry_count,
            trace_id=self._trace_id,
        )


class KiraStreamTimer:
    """Measure TTFT and full completion from one shared monotonic start."""

    def __init__(
        self,
        recorder: "TimingRecorder",
        *,
        sdk_retry_count: int,
        trace_id: str | None,
    ) -> None:
        self._recorder = recorder
        self._sdk_retry_count = sdk_retry_count
        self._trace_id = trace_id
        self._started = recorder.clock()
        self._ttft_recorded = False
        self._finished = False

    def first_token(self) -> TimingAttempt:
        if self._finished:
            raise RuntimeError("KiRa stream timing already finished")
        if self._ttft_recorded:
            raise RuntimeError("KiRa first token was already recorded")
        self._ttft_recorded = True
        return self._record(TimingStage.KIRA_TTFT, TimingOutcome.SUCCESS)

    def finish(self, outcome: TimingOutcome = TimingOutcome.SUCCESS) -> tuple[TimingAttempt, ...]:
        if self._finished:
            raise RuntimeError("KiRa stream timing already finished")
        self._finished = True
        attempts: list[TimingAttempt] = []
        if not self._ttft_recorded:
            attempts.append(self._record(TimingStage.KIRA_TTFT, outcome))
        attempts.append(self._record(TimingStage.KIRA_COMPLETION, outcome))
        return tuple(attempts)

    def _record(self, stage: TimingStage, outcome: TimingOutcome) -> TimingAttempt:
        duration_ms = max(0.0, (self._recorder.clock() - self._started) * 1000)
        return self._recorder.record(
            stage=stage,
            duration_ms=duration_ms,
            outcome=outcome,
            sdk_retry_count=self._sdk_retry_count,
            trace_id=self._trace_id,
        )


class TimingRecorder:
    """Collect local durations without calling or depending on a telemetry backend."""

    def __init__(
        self,
        *,
        clock: MonotonicClock = time.perf_counter,
        trace_id_supplier: Callable[[], str | None] | None = None,
    ) -> None:
        self.clock = clock
        self._trace_id_supplier = trace_id_supplier
        self._attempts: list[TimingAttempt] = []

    def start(
        self,
        stage: TimingStage,
        *,
        sdk_retry_count: int = 0,
        worker_retry_count: int = 0,
        trace_id: str | None = None,
    ) -> StageTimer:
        self._validate_retries(sdk_retry_count, worker_retry_count)
        return StageTimer(
            self,
            stage,
            sdk_retry_count=sdk_retry_count,
            worker_retry_count=worker_retry_count,
            trace_id=self._safe_trace_id(trace_id),
        )

    @contextmanager
    def measure(
        self,
        stage: TimingStage,
        *,
        sdk_retry_count: int = 0,
        worker_retry_count: int = 0,
        trace_id: str | None = None,
    ) -> Iterator[None]:
        timer = self.start(
            stage,
            sdk_retry_count=sdk_retry_count,
            worker_retry_count=worker_retry_count,
            trace_id=trace_id,
        )
        try:
            yield
        except TimeoutError:
            timer.finish(TimingOutcome.TIMEOUT)
            raise
        except BaseException:
            timer.finish(TimingOutcome.ERROR)
            raise
        else:
            timer.finish(TimingOutcome.SUCCESS)

    def start_kira(
        self,
        *,
        sdk_retry_count: int = 0,
        trace_id: str | None = None,
    ) -> KiraStreamTimer:
        self._validate_retries(sdk_retry_count, 0)
        return KiraStreamTimer(
            self,
            sdk_retry_count=sdk_retry_count,
            trace_id=self._safe_trace_id(trace_id),
        )

    def record(
        self,
        *,
        stage: TimingStage,
        duration_ms: float,
        outcome: TimingOutcome,
        sdk_retry_count: int = 0,
        worker_retry_count: int = 0,
        trace_id: str | None = None,
    ) -> TimingAttempt:
        self._validate_retries(sdk_retry_count, worker_retry_count)
        attempt = TimingAttempt(
            stage=stage,
            attempt=sum(item.stage is stage for item in self._attempts) + 1,
            duration_ms=duration_ms,
            outcome=outcome,
            sdk_retry_count=sdk_retry_count,
            worker_retry_count=worker_retry_count,
            trace_id=self._safe_trace_id(trace_id),
        )
        self._attempts.append(attempt)
        return attempt

    def report(self) -> TimingReport:
        summaries: list[StageTimingSummary] = []
        for stage in TimingStage:
            attempts = tuple(item for item in self._attempts if item.stage is stage)
            if not attempts:
                continue
            counts = Counter(item.outcome for item in attempts)
            durations = tuple(item.duration_ms for item in attempts)
            summaries.append(
                StageTimingSummary(
                    stage=stage,
                    attempts=len(attempts),
                    successful_attempts=counts[TimingOutcome.SUCCESS],
                    timeout_attempts=counts[TimingOutcome.TIMEOUT],
                    error_attempts=counts[TimingOutcome.ERROR],
                    sdk_retries=sum(item.sdk_retry_count for item in attempts),
                    worker_retries=sum(item.worker_retry_count for item in attempts),
                    p50_ms=nearest_rank(durations, 50),
                    p95_ms=nearest_rank(durations, 95),
                    p99_ms=nearest_rank(durations, 99),
                )
            )
        return TimingReport(attempts=tuple(self._attempts), stages=tuple(summaries))

    def _safe_trace_id(self, explicit: str | None) -> str | None:
        candidate = explicit
        if candidate is None and self._trace_id_supplier is not None:
            try:
                candidate = self._trace_id_supplier()
            except Exception:
                return None
        return candidate if isinstance(candidate, str) and _TRACE_ID.fullmatch(candidate) else None

    @staticmethod
    def _validate_retries(sdk_retry_count: int, worker_retry_count: int) -> None:
        for value in (sdk_retry_count, worker_retry_count):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("retry counts must be nonnegative integers")
