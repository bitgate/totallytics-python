"""Starlette middleware. Callbacks receive a `starlette.requests.Request`."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

from .asgi import ClicktagMiddleware as _ASGIMiddleware
from .asgi import Scope

__all__ = ["ClicktagMiddleware"]


class ClicktagMiddleware(_ASGIMiddleware):
    integration = "starlette"

    def _callback_target(self, scope: Scope) -> Any:
        return Request(scope)
