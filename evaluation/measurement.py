"""Local benchmark measurements; unknown provider usage stays unknown.

This captures no prompt, response content, endpoint, key or telemetry. SDK observations count
invocations, not unseen provider retries. HTTP hooks count requests actually made by that client.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Literal

import httpx
from pydantic import Field

from evaluation.models import EvalModel, TokenUsage
from evaluation.timing import TimingRecorder, TimingReport, TimingStage


class ProviderStage(StrEnum):
    EXTRACTION = "extraction"
    EMBEDDING = "embedding"
    REWRITE = "rewrite"
    JUDGE = "judge"
    KIRA = "kira"


class ProviderCall(EvalModel):
    stage: ProviderStage
    arm: Literal["no_ltm", "with_ltm"] | None = None
    basis: Literal["http_request", "sdk_invocation", "kira_chat"]
    outcome: Literal["success", "error"]
    usage: TokenUsage | None = None


class CaseMeasurements(EvalModel):
    provider_calls: tuple[ProviderCall, ...] = ()
    timing: dict[str, TimingReport] = Field(default_factory=dict)


def merge_measurements(
    previous: CaseMeasurements | None, current: CaseMeasurements
) -> CaseMeasurements:
    """Append a new process's measurements to a durable resume baseline exactly once."""
    if previous is None:
        return current
    timing: dict[str, TimingRecorder] = {}
    for measurement in (previous, current):
        for arm, report in measurement.timing.items():
            recorder = timing.setdefault(arm, TimingRecorder())
            for attempt in report.attempts:
                recorder.record(**attempt.model_dump(exclude={"attempt"}))
    return CaseMeasurements(
        provider_calls=(*previous.provider_calls, *current.provider_calls),
        timing={arm: recorder.report() for arm, recorder in timing.items()},
    )


_ACTIVE: ContextVar[MeasurementRecorder | None] = ContextVar("benchmark_measurement", default=None)
_ARM: ContextVar[Literal["no_ltm", "with_ltm"] | None] = ContextVar("benchmark_arm", default=None)


def token_usage(value: object) -> TokenUsage | None:
    if not isinstance(value, Mapping):
        return None
    fields = {}
    for target, aliases in {
        "prompt_tokens": ("prompt_tokens", "input_tokens", "input"),
        "completion_tokens": ("completion_tokens", "output_tokens", "output"),
        "total_tokens": ("total_tokens", "total"),
    }.items():
        for key in aliases:
            candidate = value.get(key)
            if type(candidate) is int and candidate >= 0:
                fields[target] = candidate
                break
    return TokenUsage(**fields) if fields else None


class MeasurementRecorder:
    def __init__(self) -> None:
        self._calls: list[ProviderCall] = []
        self._timing: dict[str, TimingRecorder] = {}

    @contextmanager
    def bind(self) -> Iterator[MeasurementRecorder]:
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    def call(
        self,
        stage: ProviderStage,
        *,
        basis: Literal["http_request", "sdk_invocation", "kira_chat"],
        outcome: Literal["success", "error"],
        usage: object = None,
    ) -> None:
        self._calls.append(
            ProviderCall(
                stage=stage, arm=_ARM.get(), basis=basis, outcome=outcome, usage=token_usage(usage)
            )
        )

    def timer(self) -> TimingRecorder:
        return self._timing.setdefault(_ARM.get() or "unpaired", TimingRecorder())

    def snapshot(self) -> CaseMeasurements:
        return CaseMeasurements(
            provider_calls=tuple(self._calls),
            timing={key: recorder.report() for key, recorder in self._timing.items()},
        )


def current_measurement() -> MeasurementRecorder | None:
    return _ACTIVE.get()


@contextmanager
def measurement_arm(arm: Literal["no_ltm", "with_ltm"]) -> Iterator[None]:
    token = _ARM.set(arm)
    try:
        yield
    finally:
        _ARM.reset(token)


@contextmanager
def measure_stage(stage: TimingStage) -> Iterator[None]:
    recorder = current_measurement()
    if recorder is None:
        yield
    else:
        with recorder.timer().measure(stage):
            yield


def record_provider_call(
    stage: ProviderStage,
    *,
    basis: Literal["http_request", "sdk_invocation", "kira_chat"],
    outcome: Literal["success", "error"],
    usage: object = None,
) -> None:
    recorder = current_measurement()
    if recorder is not None:
        recorder.call(stage, basis=basis, outcome=outcome, usage=usage)


@contextmanager
def provider_call(
    stage: ProviderStage,
    *,
    basis: Literal["http_request", "sdk_invocation", "kira_chat"],
) -> Iterator[dict]:
    details: dict = {"outcome": "error", "usage": None}
    try:
        yield details
    finally:
        record_provider_call(stage, basis=basis, **details)


def http_measurement_hooks(stage: ProviderStage) -> dict[str, list]:
    """Hooks for nonstreaming rewrite calls only; they preserve response content for the adapter.

    Request counts are retained even for transport errors. A request without a received response
    has unknown usage and an error outcome. Calls outside a bound recorder are ignored.
    """

    async def request_hook(request: httpx.Request) -> None:
        recorder = current_measurement()
        if recorder is None:
            return
        index = len(recorder._calls)
        recorder.call(stage, basis="http_request", outcome="error")
        request.extensions["benchmark_measurement"] = (recorder, index)

    async def response_hook(response: httpx.Response) -> None:
        record = response.request.extensions.pop("benchmark_measurement", None)
        if record is None:
            return
        recorder, index = record
        # AsyncClient.post already reads the whole response. Reading here makes usage available
        # without changing the bytes returned to the adapter; transport errors must propagate.
        await response.aread()
        try:
            body = response.json()
            usage = body.get("usage") if isinstance(body, dict) else None
        except ValueError:
            usage = None
        existing = recorder._calls[index]
        recorder._calls[index] = existing.model_copy(
            update={
                "outcome": "success" if response.is_success else "error",
                "usage": token_usage(usage),
            }
        )

    return {"request": [request_hook], "response": [response_hook]}
