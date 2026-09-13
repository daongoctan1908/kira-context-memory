import pytest
from pydantic import ValidationError

from worker.settings import WorkerSettings


def make_settings(**overrides: object) -> WorkerSettings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://worker:queue-secret@postgres.test/kira",
        "memory_database_url": "postgresql://worker:memory-secret@postgres.test/kira",
        "memory_embedding_base_url": "http://embedding.test:8002",
        "memory_embedding_model": "embedding-model",
        "memory_embedding_dims": 1024,
        "memory_llm_base_url": "http://memory-llm.test:8003",
        "memory_llm_model": "memory-model",
    }
    values.update(overrides)
    return WorkerSettings(**values)  # type: ignore[arg-type]


def test_worker_settings_read_memory_source_timezone_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_SOURCE_TIMEZONE", "Europe/Berlin")

    settings = make_settings()

    assert settings.memory_source_timezone == "Europe/Berlin"


def test_worker_settings_have_pinned_runtime_defaults_without_gateway_dependencies() -> None:
    settings = make_settings()

    assert settings.memory_job_poll_interval_seconds == 1.0
    assert settings.memory_job_batch_size == 10
    assert settings.memory_job_concurrency == 4
    assert settings.memory_job_lease_seconds == 120.0
    assert settings.memory_job_max_attempts == 5
    assert settings.memory_job_retry_delays_seconds == (1.0, 5.0, 30.0, 120.0)
    assert settings.memory_job_db_timeout_seconds == 5.0
    assert settings.memory_job_shutdown_grace_seconds == 30.0
    assert settings.memory_job_metrics_refresh_seconds == 15.0
    assert settings.memory_job_cleanup_interval_seconds == 3600.0
    assert settings.memory_job_completed_retention_seconds == 604_800.0
    assert settings.memory_job_dead_retention_seconds == 2_592_000.0
    assert settings.memory_job_cleanup_batch_size == 1000
    assert settings.memory_formation_message_limit == 10
    assert not hasattr(settings, "kira_base_url")
    assert not hasattr(settings, "kira_username")
    assert not hasattr(settings, "kira_basic_auth")
    assert not hasattr(settings, "vllm_base_url")
    assert not hasattr(settings, "vllm_model")


def test_worker_settings_read_environment_without_kira_or_rewriter(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://worker:db@postgres.test/kira")
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql://worker:memory@postgres.test/kira")
    monkeypatch.setenv("MEMORY_EMBEDDING_BASE_URL", "http://embedding.test")
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL", "embed-env")
    monkeypatch.setenv("MEMORY_EMBEDDING_DIMS", "768")
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://memory-llm.test")
    monkeypatch.setenv("MEMORY_LLM_MODEL", "memory-env")
    monkeypatch.setenv("MEMORY_JOB_CONCURRENCY", "6")
    monkeypatch.setenv("MEMORY_JOB_RETRY_DELAYS_SECONDS", "[2, 10, 45, 180]")
    monkeypatch.setenv("KIRA_BASE_URL", "not-a-url")
    monkeypatch.setenv("VLLM_BASE_URL", "not-a-url")

    settings = WorkerSettings(_env_file=None)

    assert settings.memory_embedding_model == "embed-env"
    assert settings.memory_embedding_dims == 768
    assert settings.memory_llm_model == "memory-env"
    assert settings.memory_job_concurrency == 6
    assert settings.memory_job_retry_delays_seconds == (2.0, 10.0, 45.0, 180.0)


@pytest.mark.parametrize(
    "missing_field",
    [
        "database_url",
        "memory_database_url",
        "memory_embedding_base_url",
        "memory_embedding_model",
        "memory_embedding_dims",
        "memory_llm_base_url",
        "memory_llm_model",
    ],
)
def test_worker_settings_require_every_startup_dependency(missing_field: str) -> None:
    values = make_settings().model_dump()
    values.pop(missing_field)

    with pytest.raises(ValidationError, match=missing_field):
        WorkerSettings(_env_file=None, **values)


def test_worker_settings_redact_database_and_provider_secrets() -> None:
    settings = make_settings(
        memory_embedding_api_key="embedding-secret",
        memory_llm_api_key="llm-secret",
    )

    rendered = repr(settings)
    assert "queue-secret" not in rendered
    assert "memory-secret" not in rendered
    assert "embedding-secret" not in rendered
    assert "llm-secret" not in rendered


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_job_max_attempts": 5, "memory_job_retry_delays_seconds": (1, 5)},
        {
            "memory_job_max_attempts": 3,
            "memory_job_retry_delays_seconds": (1, 0),
        },
        {
            "memory_job_max_attempts": 3,
            "memory_job_retry_delays_seconds": (1, float("inf")),
        },
    ],
)
def test_worker_settings_reject_invalid_retry_schedule(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="retry delays"):
        make_settings(**overrides)


def test_worker_settings_require_lease_longer_than_external_operation_deadlines() -> None:
    with pytest.raises(ValidationError, match="lease must exceed"):
        make_settings(
            memory_operation_timeout_seconds=30,
            conversation_operation_timeout_seconds=5,
            memory_job_lease_seconds=35,
        )

    settings = make_settings(
        memory_operation_timeout_seconds=30,
        conversation_operation_timeout_seconds=5,
        memory_job_lease_seconds=35.1,
    )
    assert settings.memory_job_lease_seconds == 35.1


def test_worker_settings_validate_memory_pool_bounds() -> None:
    with pytest.raises(ValidationError, match="max connections"):
        make_settings(
            memory_postgres_min_connections=3,
            memory_postgres_max_connections=2,
        )
