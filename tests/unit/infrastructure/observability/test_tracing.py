import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.infrastructure.observability.tracing import start_span


class FailingTracer:
    def start_as_current_span(self, *args, **kwargs):
        raise RuntimeError("observer failed")


def test_span_start_failure_degrades_to_invalid_span() -> None:
    with start_span(FailingTracer(), "test"):  # type: ignore[arg-type]
        assert trace.get_current_span() is trace.INVALID_SPAN


def test_business_exception_is_not_swallowed_by_tracing() -> None:
    tracer = TracerProvider().get_tracer("test")

    with pytest.raises(ValueError, match="business failure"):
        with start_span(tracer, "test"):
            raise ValueError("business failure")
