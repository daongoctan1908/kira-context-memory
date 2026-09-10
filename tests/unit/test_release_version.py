from importlib.metadata import version

from app.presentation.api.main import app as gateway_app
from worker.main import app as worker_app


def test_week4_service_versions_match_distribution_release() -> None:
    expected = "0.4.0"

    assert version("kira-context-memory") == expected
    assert gateway_app.version == expected
    assert worker_app.version == expected
