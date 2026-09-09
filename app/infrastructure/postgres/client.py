"""PostgreSQL async engine construction owned by the infrastructure layer."""

from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config.runtime_contracts import PostgresRuntimeSettings
from app.domain.errors.conversation import ConversationStoreConfigurationError


def create_postgres_engine(settings: PostgresRuntimeSettings) -> AsyncEngine:
    """Create a pooled SQLAlchemy async engine without opening a connection."""
    if settings.database_url is None:
        raise ConversationStoreConfigurationError

    database_url = str(settings.database_url.get_secret_value())
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)

    try:
        return create_async_engine(
            database_url,
            pool_size=settings.postgres_pool_size,
            max_overflow=settings.postgres_max_overflow,
            pool_timeout=settings.postgres_pool_timeout_seconds,
            pool_pre_ping=True,
            connect_args={
                "timeout": settings.postgres_connect_timeout_seconds,
                "command_timeout": settings.postgres_command_timeout_seconds,
            },
        )
    except (ArgumentError, ModuleNotFoundError, TypeError, ValueError) as error:
        raise ConversationStoreConfigurationError from error
