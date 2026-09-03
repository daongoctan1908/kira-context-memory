import pytest

from app.config.settings import Settings
from app.infrastructure.redis.client import create_redis_client


def make_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "kira_base_url": "http://kira.test:8122",
        "kira_username": "service-account",
        "kira_basic_auth": "secret",
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


def test_create_redis_client_requires_url() -> None:
    with pytest.raises(ValueError, match="REDIS_URL"):
        create_redis_client(make_settings())


async def test_create_redis_client_configures_pool() -> None:
    client = create_redis_client(
        make_settings(
            redis_url="redis://redis.internal:6380/3",
            redis_max_connections=17,
            redis_connect_timeout_seconds=1.5,
            redis_read_timeout_seconds=2.5,
            redis_health_check_interval_seconds=11,
        )
    )
    pool = client.connection_pool

    assert pool.max_connections == 17
    assert pool.connection_kwargs["host"] == "redis.internal"
    assert pool.connection_kwargs["port"] == 6380
    assert pool.connection_kwargs["db"] == 3
    assert pool.connection_kwargs["socket_connect_timeout"] == 1.5
    assert pool.connection_kwargs["socket_timeout"] == 2.5
    assert pool.connection_kwargs["health_check_interval"] == 11
    assert pool.connection_kwargs["decode_responses"] is True

    await client.aclose(close_connection_pool=True)
