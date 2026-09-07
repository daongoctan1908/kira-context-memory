"""Trusted identity adapter exports."""

from app.infrastructure.identity.static import NullIdentityAdapter, StaticIdentityAdapter

__all__ = ["NullIdentityAdapter", "StaticIdentityAdapter"]
