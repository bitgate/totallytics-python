"""Starlette middleware. Callbacks receive a `starlette.requests.Request`."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

from .asgi import Scope
from .asgi import TotallyticsMiddleware as _ASGIMiddleware

__all__ = ["TotallyticsMiddleware"]


class TotallyticsMiddleware(_ASGIMiddleware):
    integration = "starlette"

    def _callback_target(self, scope: Scope) -> Any:
        return Request(scope)
