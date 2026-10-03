from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricsData

from app.infrastructure.observability.metrics import (
    LEGACY_METRIC_MAP,
    METRIC_SPECS,
    GatewayMetrics,
    WorkerMetrics,
    bounded_metric_attributes,
)


def metric_items(data: MetricsData) -> Iterator[Any]:
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            yield from scope_metrics.metrics


def metric_by_name(reader: InMemoryMetricReader, name: str) -> Any:
    data = reader.get_metrics_data()
    assert data is not None
    return next(metric for metric in metric_items(data) if metric.name == name)


def test_every_legacy_metric_has_one_reviewed_otel_spec() -> None:
    spec_names = [spec.name for spec in METRIC_SPECS.values()]

    assert len(LEGACY_METRIC_MAP) == 22
    assert set(LEGACY_METRIC_MAP.values()) <= set(spec_names)
    assert all(spec_names.count(name) == 1 for name in LEGACY_METRIC_MAP.values())


@pytest.mark.parametrize(
    "forbidden",
    (
        "correlation_id",
        "trace_id",
        "span_id",
        "user_id",
        "session_id",
        "conversation_id",
        "turn_id",
        "boundary_message_id",
        "provider_request_id",
        "memory_id",
        "event_id",
        "content",
        "prompt",
        "response",
    ),
)
def test_metric_attributes_reject_all_identifier_dimensions(forbidden: str) -> None:
    with pytest.raises(ValueError, match="reviewed bounded enums"):
        bounded_metric_attributes({forbidden: "private-value"})


def test_gateway_metric_count_outcome_units_and_buckets_match_contract() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    observed = GatewayMetrics(provider.get_meter("gateway-test"))

    observed.memory_search_observed("success", 2, 0.03)
    observed.request_observed("degraded", 0.4)

    count = metric_by_name(reader, "kira.memory.search.count")
    duration = metric_by_name(reader, "kira.memory.search.duration")
    request = metric_by_name(reader, "kira.chat.request.duration")

    assert count.unit == "{search}"
    assert count.data.data_points[0].attributes == {"outcome": "success"}
    assert count.data.data_points[0].value == 1
    assert duration.unit == "s"
    assert duration.data.data_points[0].explicit_bounds == (
        0.01,
        0.05,
        0.1,
        0.5,
        1,
        2,
        3,
        5,
    )
    assert request.data.data_points[0].attributes == {"outcome": "degraded"}
    provider.shutdown()


@pytest.mark.parametrize("outcome", ("active", "inactive"))
def test_gateway_records_conversation_activity_stage(outcome: str) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    observed = GatewayMetrics(provider.get_meter("gateway-test"))

    observed.stage_observed("conversation.check_active", outcome, 0.03)

    point = metric_by_name(reader, "kira.stage.duration").data.data_points[0]
    assert point.attributes == {"stage": "conversation.check_active", "outcome": outcome}
    assert point.count == 1
    assert point.sum == pytest.approx(0.03)
    provider.shutdown()


@pytest.mark.parametrize(
    ("stage", "outcome"),
    (
        ("mem0.extract.scope", "enforced"),
        ("mem0.persist", "scope_dropped"),
        ("memory_job.transition", "skipped"),
    ),
)
def test_worker_records_scope_and_skipped_job_stages(stage: str, outcome: str) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    observed = WorkerMetrics(provider.get_meter("worker-test"))

    observed.stage_observed(stage, outcome, 0.02)

    point = metric_by_name(reader, "kira.stage.duration").data.data_points[0]
    assert point.attributes == {"stage": stage, "outcome": outcome}
    assert point.count == 1
    assert point.sum == pytest.approx(0.02)
    provider.shutdown()


def test_worker_records_skipped_jobs_in_processing_count_and_duration() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    observed = WorkerMetrics(provider.get_meter("worker-test"))

    observed.job_processed(
        outcome="skipped",
        seconds=0.04,
        attempt_count=1,
        lifecycle_event_count=None,
    )

    count = metric_by_name(reader, "kira.memory.job.process.count").data.data_points[0]
    duration = metric_by_name(reader, "kira.memory.job.process.duration").data.data_points[0]
    assert count.attributes == {"outcome": "skipped"}
    assert count.value == 1
    assert duration.attributes == {"outcome": "skipped"}
    assert duration.count == 1
    assert duration.sum == pytest.approx(0.04)
    provider.shutdown()


def test_worker_observable_gauges_read_only_cached_snapshot() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    observed = WorkerMetrics(provider.get_meter("worker-test"))

    observed.queue_stats_observed(
        pending=4,
        processing=3,
        completed=2,
        dead=1,
        oldest_pending_age_seconds=9.5,
    )
    observed.runtime_observed(
        runner_active=True,
        queue_database_available=True,
        in_flight_count=3,
        database_backoff_seconds=0.25,
    )
    observed.queue_wait_observed(4.0)

    queue = metric_by_name(reader, "kira.memory.job.queue.depth")
    values = {point.attributes["status"]: point.value for point in queue.data.data_points}

    assert values == {"pending": 4, "processing": 3, "completed": 2, "dead": 1}
    assert metric_by_name(reader, "kira.memory.worker.runner.active").data.data_points[0].value == 1
    assert (
        metric_by_name(reader, "kira.memory.job.queue.database.available").data.data_points[0].value
        == 1
    )
    assert observed.cached_snapshot.in_flight_count == 3
    assert metric_by_name(reader, "kira.memory.job.queue_wait.duration").unit == "s"
    provider.shutdown()


def test_metrics_restart_with_an_independent_provider_state() -> None:
    first_reader = InMemoryMetricReader()
    first_provider = MeterProvider(metric_readers=[first_reader])
    first = GatewayMetrics(first_provider.get_meter("gateway-test"))
    first.memory_job_schedule_observed("scheduled")

    second_reader = InMemoryMetricReader()
    second_provider = MeterProvider(metric_readers=[second_reader])
    GatewayMetrics(second_provider.get_meter("gateway-test"))

    assert (
        metric_by_name(first_reader, "kira.memory.job.schedule.count").data.data_points[0].value
        == 1
    )
    second_data = second_reader.get_metrics_data()
    assert second_data is None or not any(metric_items(second_data))
    first_provider.shutdown()
    second_provider.shutdown()


class BrokenMeter:
    def create_counter(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("private exporter detail")

    create_histogram = create_counter
    create_observable_gauge = create_counter


def test_metric_initialization_and_recording_fail_open() -> None:
    gateway = GatewayMetrics(BrokenMeter())  # type: ignore[arg-type]
    worker = WorkerMetrics(BrokenMeter())  # type: ignore[arg-type]

    gateway.memory_search_observed("success", 1, 0.2)
    gateway.request_observed("success", 0.3)
    worker.jobs_claimed(new_count=1, reclaimed_count=0)
    worker.queue_stats_observed(
        pending=1,
        processing=0,
        completed=0,
        dead=0,
        oldest_pending_age_seconds=0.0,
    )
