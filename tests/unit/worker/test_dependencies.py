from unittest.mock import Mock

import pytest

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import ProcessMemoryJobUseCase
from app.domain.errors.conversation import ConversationStoreConfigurationError
from app.domain.errors.memory import LongTermMemoryConfigurationError
from app.domain.errors.memory_job import MemoryJobQueueConfigurationError
from app.infrastructure.postgres.managed_store import ManagedPostgresConversationStore
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import EXPECTED_SCHEMA_REVISION
from worker.dependencies import worker_dependency_lifespan
from worker.runner import MemoryJobRunner
from worker.settings import WorkerSettings


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
    }
    values.update(overrides)
    return WorkerSettings(**values)  # type: ignore[arg-type]


class FakeConnection:
    def __init__(self, revisions: list[object] | None = None) -> None:
        self.revisions = list(revisions or [EXPECTED_SCHEMA_REVISION, EXPECTED_SCHEMA_REVISION])

    async def scalar(self, statement: object) -> object:
        return self.revisions.pop(0)


class FakeConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeEngine:
    def __init__(self, revisions: list[object] | None = None) -> None:
        self.connection = FakeConnection(revisions)
        self.disposed = False

    def connect(self) -> FakeConnectionContext:
        return FakeConnectionContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


class FakeMemory:
    def __init__(self) -> None:
        self.closed = False

    async def search(self, *args: object, **kwargs: object) -> tuple[object, ...]:
        return ()

    async def process_memory(self, source: object) -> object:
        raise AssertionError("T4.11 must not process jobs")

    def close(self) -> None:
        self.closed = True


async def valid_memory_schema(settings: WorkerSettings) -> None:
    return None


async def test_lifespan_constructs_validated_worker_dependencies_and_closes_owned_resources(
    monkeypatch,
) -> None:
    engine = FakeEngine()
    memory = FakeMemory()
    engine_factory = Mock(return_value=engine)
    memory_factory = Mock(return_value=memory)
    memory_schema_validator = Mock(side_effect=valid_memory_schema)
    monkeypatch.setattr("worker.dependencies.create_postgres_engine", engine_factory)
    monkeypatch.setattr("worker.dependencies.Mem0Adapter.from_settings", memory_factory)
    monkeypatch.setattr("worker.dependencies.validate_memory_schema", memory_schema_validator)
    settings = make_settings(memory_formation_message_limit=8)

    async with worker_dependency_lifespan(settings) as dependencies:
        assert dependencies.settings is settings
        assert isinstance(dependencies.conversation_store, ManagedPostgresConversationStore)
        assert isinstance(dependencies.memory_job_queue, PostgresMemoryJobQueueAdapter)
        assert dependencies.long_term_memory is memory
        assert isinstance(dependencies.process_memory, ProcessMemoryUseCase)
        assert isinstance(dependencies.process_memory_job, ProcessMemoryJobUseCase)
        assert isinstance(dependencies.runner, MemoryJobRunner)
        assert dependencies.process_memory._message_limit == 8
        assert memory.closed is False
        assert engine.disposed is False

    engine_factory.assert_called_once_with(settings)
    memory_factory.assert_called_once_with(settings)
    memory_schema_validator.assert_called_once_with(settings)
    assert memory.closed is True
    assert engine.disposed is True


async def test_lifespan_does_not_close_injected_dependencies(monkeypatch) -> None:
    engine = FakeEngine()
    memory = FakeMemory()
    monkeypatch.setattr("worker.dependencies.validate_memory_schema", valid_memory_schema)

    async with worker_dependency_lifespan(
        make_settings(),
        postgres_engine=engine,  # type: ignore[arg-type]
        long_term_memory=memory,  # type: ignore[arg-type]
    ) as dependencies:
        assert dependencies.long_term_memory is memory

    assert memory.closed is False
    assert engine.disposed is False


async def test_lifespan_fails_before_mem0_when_conversation_schema_is_wrong(monkeypatch) -> None:
    engine = FakeEngine(["stale-revision"])
    memory_factory = Mock()
    memory_schema_validator = Mock(side_effect=valid_memory_schema)
    monkeypatch.setattr("worker.dependencies.create_postgres_engine", Mock(return_value=engine))
    monkeypatch.setattr("worker.dependencies.Mem0Adapter.from_settings", memory_factory)
    monkeypatch.setattr("worker.dependencies.validate_memory_schema", memory_schema_validator)

    with pytest.raises(ConversationStoreConfigurationError):
        async with worker_dependency_lifespan(make_settings()):
            raise AssertionError("invalid schema must fail before yielding")

    memory_factory.assert_not_called()
    memory_schema_validator.assert_not_called()
    assert engine.disposed is True


async def test_lifespan_fails_before_mem0_when_queue_schema_is_wrong(monkeypatch) -> None:
    engine = FakeEngine([EXPECTED_SCHEMA_REVISION, "stale-revision"])
    memory_factory = Mock()
    memory_schema_validator = Mock(side_effect=valid_memory_schema)
    monkeypatch.setattr("worker.dependencies.create_postgres_engine", Mock(return_value=engine))
    monkeypatch.setattr("worker.dependencies.Mem0Adapter.from_settings", memory_factory)
    monkeypatch.setattr("worker.dependencies.validate_memory_schema", memory_schema_validator)

    with pytest.raises(MemoryJobQueueConfigurationError):
        async with worker_dependency_lifespan(make_settings()):
            raise AssertionError("invalid schema must fail before yielding")

    memory_factory.assert_not_called()
    memory_schema_validator.assert_not_called()
    assert engine.disposed is True


async def test_lifespan_fails_before_mem0_when_memory_schema_is_wrong(monkeypatch) -> None:
    engine = FakeEngine()
    memory_factory = Mock()

    async def invalid_memory_schema(settings: WorkerSettings) -> None:
        raise LongTermMemoryConfigurationError

    monkeypatch.setattr("worker.dependencies.create_postgres_engine", Mock(return_value=engine))
    monkeypatch.setattr("worker.dependencies.Mem0Adapter.from_settings", memory_factory)
    monkeypatch.setattr("worker.dependencies.validate_memory_schema", invalid_memory_schema)

    with pytest.raises(LongTermMemoryConfigurationError):
        async with worker_dependency_lifespan(make_settings()):
            raise AssertionError("invalid schema must fail before yielding")

    memory_factory.assert_not_called()
    assert engine.disposed is True


async def test_lifespan_disposes_engine_when_mem0_construction_fails(monkeypatch) -> None:
    engine = FakeEngine()
    monkeypatch.setattr("worker.dependencies.create_postgres_engine", Mock(return_value=engine))
    monkeypatch.setattr("worker.dependencies.validate_memory_schema", valid_memory_schema)
    monkeypatch.setattr(
        "worker.dependencies.Mem0Adapter.from_settings",
        Mock(side_effect=LongTermMemoryConfigurationError),
    )

    with pytest.raises(LongTermMemoryConfigurationError):
        async with worker_dependency_lifespan(make_settings()):
            raise AssertionError("invalid Mem0 config must fail before yielding")

    assert engine.disposed is True
