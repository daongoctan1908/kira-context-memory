"""Outbound ports owned by the domain layer."""

from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.kira_client import KiraClientPort

__all__ = ["ConversationStorePort", "KiraClientPort"]
