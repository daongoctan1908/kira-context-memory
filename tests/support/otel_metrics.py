"""In-memory OTel observations for behavior tests, without a scrape/export path."""

from dataclasses import dataclass

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.infrastructure.observability.context import ContextTelemetry
from app.infrastructure.observability.runtime import ObservabilityRuntime
from worker.telemetry import MemoryJobTelemetry

_CAPTURES = []


class RetainedMetricReader(InMemoryMetricReader):
    """Keep the final observation available after an application lifespan shuts down."""

    final_data = None

    def shutdown(self, timeout_millis=30_000, **kwargs):
        self.final_data = self.get_metrics_data()
        super().shutdown(timeout_millis=timeout_millis, **kwargs)


@dataclass(frozen=True)
class MetricSnapshot:
    points: dict[str, tuple[object, ...]]

    def value(self, name, attributes=None, *, field="value"):
        points = self.points.get(name, ())
        selected = [
            point for point in points if attributes is None or dict(point.attributes) == attributes
        ]
        if not selected:
            return None
        return sum(getattr(point, field) for point in selected)


class MetricCapture:
    def __init__(self):
        self.reader = RetainedMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])
        self.meter = self.provider.get_meter("kira.behavior-test")
        self.closed = False
        _CAPTURES.append(self)

    def snapshot(self):
        data = self.reader.final_data or self.reader.get_metrics_data()
        points = {}
        if data is not None:
            for resource in data.resource_metrics:
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        points[metric.name] = tuple(metric.data.data_points)
        return MetricSnapshot(points)

    def value(self, name, attributes=None, *, field="value"):
        return self.snapshot().value(name, attributes, field=field)

    def close(self):
        if not self.closed:
            if self.reader.final_data is None:
                self.provider.shutdown()
            self.closed = True


def gateway_telemetry():
    capture = MetricCapture()
    return ContextTelemetry(meter=capture.meter), capture


def worker_telemetry():
    capture = MetricCapture()
    return MemoryJobTelemetry(meter=capture.meter), capture


@pytest.fixture(autouse=True)
def close_metric_captures():
    yield
    while _CAPTURES:
        _CAPTURES.pop().close()


@pytest.fixture
def otel_capture(monkeypatch):
    """Inject a real OTel meter while keeping API tests independent of OTLP networking."""
    import app.presentation.api.main as gateway_main
    import worker.main as worker_main

    captures = []

    def runtime_factory(settings):
        capture = MetricCapture()
        captures.append(capture)
        return ObservabilityRuntime(settings=settings, meter_provider=capture.provider)

    monkeypatch.setattr(gateway_main, "create_observability_runtime", runtime_factory)
    monkeypatch.setattr(worker_main, "create_observability_runtime", runtime_factory)

    class Applications:
        def snapshot(self):
            assert captures, "the application lifespan must create an OTel runtime"
            return captures[-1].snapshot()

    yield Applications()
    for capture in captures:
        if capture.reader.final_data is None:
            capture.close()
