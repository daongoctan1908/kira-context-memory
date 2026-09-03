import ast
from pathlib import Path

from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.kira_client import KiraClientPort


def test_kira_client_port_is_a_protocol() -> None:
    assert KiraClientPort.__name__ == "KiraClientPort"


def test_conversation_store_port_is_a_protocol() -> None:
    assert ConversationStorePort.__name__ == "ConversationStorePort"


def test_core_layers_do_not_import_infrastructure_frameworks() -> None:
    forbidden_roots = {"fastapi", "httpx", "pydantic_settings", "redis"}
    core_roots = [Path("app/domain"), Path("app/application")]

    violations: list[str] = []
    for root in core_roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = {node.module.split(".", maxsplit=1)[0]}
                else:
                    continue
                if imported & forbidden_roots:
                    violations.append(f"{path}: {sorted(imported & forbidden_roots)}")

    assert violations == []
