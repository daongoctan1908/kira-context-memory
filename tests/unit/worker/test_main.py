import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import worker.main as worker_main
from app.domain.errors.memory_job import MemoryJobQueueConfigurationError
from app.domain.models.memory_job import MemoryJobStats
from worker.main import create_app
from worker.runner import MemoryJobRunnerSnapshot
from worker.settings import WorkerSettings

NOW = datetime(2026, 9, 9, 17, 0, tzinfo=UTC)


def make_settings(**overrides: object) -> WorkerSettings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://worker:secret@postgres.test/kira",
        "memory_database_url": "postgresql://worker:secret@postgres.test/kira",
        "memory_embedding_base_url": "http://embedding.test",
        "memory_embedding_model": "embedding-model",
        "memory_embedding_dims": 3,
        "memory_llm_base_url": "http://memory-llm.test",
        "memory_llm_model": "memory-model",
        "memory_job_metrics_refresh_seconds": 100,
        "memory_job_db_timeout_seconds": 0.1,
    }
    values.update(overrides)
    return WorkerSettings(**values)  # type: ignore[arg-type]


class FakeRunner:
    def __init__(self, *, database_available: bool = True) -> None:
        self.database_available = database_available
        self._snapshot = MemoryJobRunnerSnapshot(False, False, False, None, 0, 0.0, 0)
        self.stop_requested = asyncio.Event()

    @property
    def snapshot(self) -> MemoryJobRunnerSnapshot:
        return self._snapshot

    async def run(self) -> None:
        self._snapshot = replace(
            self._snapshot,
            running=True,
            database_available=self.database_available,
            last_successful_poll_at=NOW if self.database_available else None,
        )
        try:
            await self.stop_requested.wait()
        finally:
            self._snapshot = replace(self._snapshot, running=False, stopping=True)

    def request_stop(self) -> None:
        self.stop_requested.set()


class FakeQueue:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.stats_calls = 0
        self.sampled = asyncio.Event()

    async def stats(self) -> MemoryJobStats:
        self.stats_calls += 1
        if self.error is not None:
            self.sampled.set()
            raise self.error
        self.sampled.set()
        return MemoryJobStats(2, 1, 4, 3, 7.5)


def dependency_factory(runner: FakeRunner, queue: FakeQueue, captured: dict[str, object]):
    @asynccontextmanager
    async def dependencies(settings, **kwargs):
        captured["settings"] = settings
        captured.update(kwargs)
        yield SimpleNamespace(runner=runner, memory_job_queue=queue)

    return dependencies


async def test_worker_app_exposes_only_internal_read_endpoints_and_cached_metrics() -> None:
    settings = make_settings()
    runner = FakeRunner()
    queue = FakeQueue()
    captured: dict[str, object] = {}
    app = create_app(
        settings=settings,
        dependency_lifespan=dependency_factory(runner, queue, captured),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker.test") as client:
            await asyncio.wait_for(queue.sampled.wait(), timeout=1)
            await asyncio.sleep(0)
            ready = await client.get("/ready")
            calls_before_probes = queue.stats_calls
            health = await client.get("/health")
            metrics = await client.get("/metrics")

        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert ready.json() == {"status": "ready"}
        assert metrics.status_code == 200
        assert metrics.headers["content-type"].startswith("text/plain")
        assert 'kira_memory_job_queue_depth{status="pending"} 2.0' in metrics.text
        assert "kira_memory_job_oldest_pending_age_seconds 7.5" in metrics.text
        assert "kira_memory_worker_runner_active 1.0" in metrics.text
        assert "kira_memory_job_queue_database_available 1.0" in metrics.text
        assert queue.stats_calls == calls_before_probes

    assert runner.stop_requested.is_set()
    assert captured["settings"] is settings
    assert captured["job_observer"] is app.state.telemetry
    exposed = {
        (method, route.path) for route in app.routes for method in getattr(route, "methods", set())
    }
    assert exposed == {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/metrics"),
    }


async def test_health_stays_live_while_queue_readiness_is_degraded() -> None:
    runner = FakeRunner(database_available=False)
    queue = FakeQueue(error=RuntimeError("private database detail"))
    app = create_app(
        settings=make_settings(),
        dependency_lifespan=dependency_factory(runner, queue, {}),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker.test") as client:
            await asyncio.sleep(0)
            health = await client.get("/health")
            ready = await client.get("/ready")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready"}


async def test_ready_is_503_before_lifespan_initialization() -> None:
    app = create_app(
        settings=make_settings(),
        dependency_lifespan=dependency_factory(FakeRunner(), FakeQueue(), {}),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://worker.test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


async def test_configuration_failure_still_fails_worker_startup() -> None:
    @asynccontextmanager
    async def invalid_dependencies(*args, **kwargs):
        raise MemoryJobQueueConfigurationError
        yield  # pragma: no cover

    app = create_app(settings=make_settings(), dependency_lifespan=invalid_dependencies)

    with pytest.raises(MemoryJobQueueConfigurationError):
        async with app.router.lifespan_context(app):
            raise AssertionError("invalid configuration must not start the Worker")


def test_module_entrypoint_runs_dedicated_worker_host_and_port(monkeypatch) -> None:
    settings = make_settings(worker_host="127.0.0.2", worker_port=8123, worker_log_level="WARNING")
    uvicorn_run = Mock()
    monkeypatch.setattr(worker_main, "get_worker_settings", Mock(return_value=settings))
    monkeypatch.setattr(worker_main.uvicorn, "run", uvicorn_run)

    worker_main.main()

    uvicorn_run.assert_called_once_with(
        worker_main.app,
        host="127.0.0.2",
        port=8123,
        log_level="warning",
    )
