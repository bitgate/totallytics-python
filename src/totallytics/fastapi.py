"""FastAPI middleware. Callbacks receive a `fastapi.Request`."""

from __future__ import annotations

from .starlette import TotallyticsMiddleware as _StarletteMiddleware

__all__ = ["TotallyticsMiddleware"]


class TotallyticsMiddleware(_StarletteMiddleware):
    integration = "fastapi"
