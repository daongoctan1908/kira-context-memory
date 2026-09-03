"""FastAPI routers and application factory."""

from app.presentation.api.main import app, create_app

__all__ = ["app", "create_app"]
