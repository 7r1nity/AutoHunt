"""Stable ASGI entry point; HTTP composition lives in app.api.application."""

from .api.application import app

__all__ = ["app"]
