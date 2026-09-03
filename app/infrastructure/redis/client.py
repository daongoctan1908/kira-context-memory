"""Redis client construction owned by the infrastructure layer."""

from redis.asyncio import ConnectionPool, Redis

from app.config.settings import Settings


def create_redis_client(settings: Settings) -> Redis:
    """Create a pooled async Redis client from environment-backed settings."""
    if settings.redis_url is None:
        raise ValueError("REDIS_URL is required to create a Redis client")

    pool = ConnectionPool.from_url(
        str(settings.redis_url.get_secret_value()),
        decode_responses=True,
        max_connections=settings.redis_max_connections,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_read_timeout_seconds,
        health_check_interval=settings.redis_health_check_interval_seconds,
    )
    return Redis(connection_pool=pool)
