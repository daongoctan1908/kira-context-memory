"""Internal FastAPI process for asynchronous memory-job execution."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.ports.long_term_memory import LongTermMemoryPort
from worker.cleanup import MemoryJobCleanupRunner
from worker.dependencies import WorkerDependencies, worker_dependency_lifespan
from worker.runtime import MemoryWorkerRuntime
from worker.settings import WorkerSettings, get_worker_settings
from worker.telemetry import MemoryJobTelemetry, configure_worker_logging

WorkerDependencyLifespan = Callable[..., AbstractAsyncContextManager[WorkerDependencies]]


def create_app(
    *,
    settings: WorkerSettings | None = None,
    postgres_engine: AsyncEngine | None = None,
    long_term_memory: LongTermMemoryPort | None = None,
    dependency_lifespan: WorkerDependencyLifespan = worker_dependency_lifespan,
) -> FastAPI:
    """Create the internal Worker app with explicit dependency injection for tests."""
    telemetry = MemoryJobTelemetry()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or get_worker_settings()
        configure_worker_logging(resolved_settings.worker_log_level)
        async with dependency_lifespan(
            resolved_settings,
            postgres_engine=postgres_engine,
            long_term_memory=long_term_memory,
            job_observer=telemetry,
        ) as dependencies:
            cleanup_runner = MemoryJobCleanupRunner(
                dependencies.memory_job_queue,
                interval_seconds=resolved_settings.memory_job_cleanup_interval_seconds,
                completed_retention_seconds=(
                    resolved_settings.memory_job_completed_retention_seconds
                ),
                dead_retention_seconds=resolved_settings.memory_job_dead_retention_seconds,
                batch_size=resolved_settings.memory_job_cleanup_batch_size,
                database_timeout_seconds=resolved_settings.memory_job_db_timeout_seconds,
                observer=telemetry,
            )
            runtime = MemoryWorkerRuntime(
                dependencies.runner,
                dependencies.memory_job_queue,
                telemetry,
                metrics_refresh_seconds=resolved_settings.memory_job_metrics_refresh_seconds,
                database_timeout_seconds=resolved_settings.memory_job_db_timeout_seconds,
                cleanup_runner=cleanup_runner,
            )
            application.state.runtime = runtime
            await runtime.start()
            try:
                yield
            finally:
                await runtime.stop()
                application.state.runtime = None

    application = FastAPI(
        title="KiRa Memory Worker",
        version="0.4.1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.runtime = None
    application.state.telemetry = telemetry

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Report that the Worker event loop is serving HTTP requests."""
        return {"status": "ok"}

    @application.get("/ready", tags=["health"])
    async def ready(request: Request) -> JSONResponse:
        """Require an active runner and a fresh PostgreSQL queue observation."""
        runtime = request.app.state.runtime
        is_ready = runtime is not None and runtime.is_ready
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "not_ready"},
        )

    @application.get("/metrics", response_class=Response, tags=["metrics"])
    async def metrics(request: Request) -> Response:
        """Render cached process metrics without querying dependencies."""
        runtime = request.app.state.runtime
        if runtime is not None:
            runtime.observe_runtime()
        return Response(
            generate_latest(request.app.state.telemetry.registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    return application


app = create_app()


def main() -> None:
    """Run the Worker HTTP process with its dedicated host and port settings."""
    settings = get_worker_settings()
    uvicorn.run(
        app,
        host=settings.worker_host,
        port=settings.worker_port,
        log_level=settings.worker_log_level.lower(),
    )


if __name__ == "__main__":
    main()
