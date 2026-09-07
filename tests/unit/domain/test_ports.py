import ast
from pathlib import Path

from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.identity import IdentityPort
from app.domain.ports.kira_client import KiraClientPort
from app.domain.ports.long_term_memory import LongTermMemoryPort
from app.domain.ports.query_rewriter import QueryRewriterPort


def test_kira_client_port_is_a_protocol() -> None:
    assert KiraClientPort.__name__ == "KiraClientPort"


def test_conversation_store_port_is_a_protocol() -> None:
    assert ConversationStorePort.__name__ == "ConversationStorePort"


def test_query_rewriter_port_is_a_protocol() -> None:
    assert QueryRewriterPort.__name__ == "QueryRewriterPort"


def test_identity_and_long_term_memory_ports_are_protocols() -> None:
    assert IdentityPort.__name__ == "IdentityPort"
    assert LongTermMemoryPort.__name__ == "LongTermMemoryPort"


def test_core_layers_do_not_import_infrastructure_frameworks() -> None:
    forbidden_roots = {
        "alembic",
        "asyncpg",
        "fastapi",
        "httpx",
        "mem0",
        "pydantic_settings",
        "psycopg",
        "prometheus_client",
        "redis",
        "sqlalchemy",
    }
    core_roots = [Path("app/domain"), Path("app/application")]

    violations: list[str] = []
    for root in core_roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = {node.module}
                else:
                    continue
                imported = {module.split(".", maxsplit=1)[0] for module in modules}
                if imported & forbidden_roots:
                    violations.append(f"{path}: {sorted(imported & forbidden_roots)}")
                if any(
                    module == "app.infrastructure" or module.startswith("app.infrastructure.")
                    for module in modules
                ):
                    violations.append(f"{path}: core imports an infrastructure adapter")

    assert violations == []
