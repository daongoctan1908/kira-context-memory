from unittest.mock import Mock

import pytest

from app.config.settings import Settings
from app.domain.errors.conversation import ConversationStoreConfigurationError
from app.infrastructure.postgres.client import create_postgres_engine


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "kira_base_url": "http://kira.test:8122",
        "kira_username": "service-account",
        "kira_basic_auth": "secret",
        "database_url": "postgresql://gateway:db-secret@postgres.test:5432/kira",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_create_engine_normalizes_url_and_configures_pool(monkeypatch) -> None:
    factory = Mock(return_value=object())
    monkeypatch.setattr("app.infrastructure.postgres.client.create_async_engine", factory)
    settings = make_settings(
        postgres_pool_size=7,
        postgres_max_overflow=3,
        postgres_pool_timeout_seconds=1.5,
        postgres_connect_timeout_seconds=1.25,
        postgres_command_timeout_seconds=4.0,
    )

    result = create_postgres_engine(settings)

    assert result is factory.return_value
    factory.assert_called_once_with(
        "postgresql+asyncpg://gateway:db-secret@postgres.test:5432/kira",
        pool_size=7,
        max_overflow=3,
        pool_timeout=1.5,
        pool_pre_ping=True,
        connect_args={"timeout": 1.25, "command_timeout": 4.0},
    )


def test_create_engine_accepts_explicit_asyncpg_url(monkeypatch) -> None:
    factory = Mock(return_value=object())
    monkeypatch.setattr("app.infrastructure.postgres.client.create_async_engine", factory)
    settings = make_settings(
        database_url="postgresql+asyncpg://gateway:secret@postgres.test:5432/kira"
    )

    create_postgres_engine(settings)

    assert factory.call_args.args[0].startswith("postgresql+asyncpg://")


def test_create_engine_requires_database_url() -> None:
    with pytest.raises(ConversationStoreConfigurationError):
        create_postgres_engine(make_settings(database_url=None))


def test_create_engine_maps_invalid_driver_configuration(monkeypatch) -> None:
    factory = Mock(side_effect=ValueError("contains private URL"))
    monkeypatch.setattr("app.infrastructure.postgres.client.create_async_engine", factory)

    with pytest.raises(ConversationStoreConfigurationError) as captured:
        create_postgres_engine(make_settings())

    assert "db-secret" not in str(captured.value)
