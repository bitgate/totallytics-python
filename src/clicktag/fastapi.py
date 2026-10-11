"""FastAPI middleware. Callbacks receive a `fastapi.Request`."""

from __future__ import annotations

from .starlette import ClicktagMiddleware as _StarletteMiddleware

__all__ = ["ClicktagMiddleware"]


class ClicktagMiddleware(_StarletteMiddleware):
    integration = "fastapi"
