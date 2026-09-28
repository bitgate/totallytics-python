"""Generic ASGI middleware. Callbacks receive the ASGI `scope`."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, MutableMapping
from typing import Any, Callable

from ._client import SHUTDOWN_BUDGET_SECONDS
from ._middleware import ApiKey, Middleware, join_route, starlette_template
from ._util import describe_error

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

CLIENT_CLOSED_REQUEST = 499
SHUTDOWN_MESSAGES = ("lifespan.shutdown.complete", "lifespan.shutdown.failed")

__all__ = ["TotallyticsMiddleware"]


class _Exchange:
    __slots__ = ("disconnected", "duration_ms", "path", "root_path", "start", "started_at", "status")

    def __init__(self, scope: Scope) -> None:
        self.started_at = time.time_ns() // 1_000_000
        self.start = time.perf_counter()
        self.status: int | None = None
        self.duration_ms: float | None = None
        self.disconnected = False
        self.path: str = scope.get("path") or "/"
        self.root_path: str = scope.get("root_path") or ""

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000


class TotallyticsMiddleware(Middleware):
    """Records every HTTP request. Duration runs until the response starts, so streamed bodies are excluded."""

    integration = "asgi"

    def __init__(self, app: ASGIApp, api_key: ApiKey = None, **options: Any) -> None:
        super().__init__(api_key, **options)
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, self._flush_on_shutdown(send))
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        exchange = _Exchange(scope)

        async def watch_receive() -> Message:
            message = await receive()
            if message.get("type") == "http.disconnect":
                exchange.disconnected = True
            return message

        async def watch_send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                exchange.status = message.get("status")
                exchange.duration_ms = exchange.elapsed_ms()
            await send(message)

        try:
            await self.app(scope, watch_receive, watch_send)
        except BaseException as error:
            self._finish(scope, exchange, error)
            raise
        self._finish(scope, exchange, None)

    def _finish(self, scope: Scope, exchange: _Exchange, error: BaseException | None) -> None:
        try:
            status = exchange.status
            if status is None:
                # We never answered: the client went away, the task was cancelled, or the app crashed
                cancelled = error is not None and not isinstance(error, Exception)
                status = CLIENT_CLOSED_REQUEST if exchange.disconnected or cancelled else 500

            self._track(
                self._callback_target(scope),
                method=scope.get("method") or "GET",
                path=exchange.path,
                template=_route_template(scope, exchange),
                status=status,
                duration_ms=exchange.duration_ms if exchange.duration_ms is not None else exchange.elapsed_ms(),
                started_at=exchange.started_at,
                user_agent=_user_agent(scope),
                error=error if isinstance(error, Exception) else None,
            )
        except Exception as failure:
            self._log(f"tracking failed: {describe_error(failure)}")

    def _callback_target(self, scope: Scope) -> Any:
        return scope

    def _flush_on_shutdown(self, send: Send) -> Send:
        async def send_after_flush(message: Message) -> None:
            if message.get("type") in SHUTDOWN_MESSAGES:
                await _run_blocking(self.client.flush, SHUTDOWN_BUDGET_SECONDS)
            await send(message)

        return send_after_flush


async def _run_blocking(function: Callable[[float], bool], argument: float) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        function(argument)
        return
    await loop.run_in_executor(None, function, argument)


def _user_agent(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == b"user-agent":
            return value.decode("latin-1")
    return None


def _route_template(scope: Scope, exchange: _Exchange) -> str | None:
    route = scope.get("route")
    path_format = getattr(route, "path_format", None)
    if not isinstance(path_format, str):
        return None

    # Mounts append the matched prefix to root_path while routing
    root_path: str = scope.get("root_path") or ""
    mount_prefix = root_path[len(exchange.root_path) :] if root_path.startswith(exchange.root_path) else ""

    effective_format = _fastapi_effective_format(scope, route)
    if effective_format is not None:
        return join_route(exchange.root_path + mount_prefix, starlette_template(effective_format))

    relative_path = exchange.path
    if exchange.root_path and relative_path.startswith(exchange.root_path):
        relative_path = relative_path[len(exchange.root_path) :]
    level = _route_level(getattr(route, "path_regex", None), mount_prefix, relative_path)
    return join_route(exchange.root_path + level, starlette_template(path_format))


# FastAPI keeps include_router prefixes out of route.path_format since 0.13x
def _fastapi_effective_format(scope: Scope, route: object) -> str | None:
    state = scope.get("fastapi")
    context = state.get("effective_route_context") if isinstance(state, dict) else None
    if context is None or getattr(context, "original_route", None) is not route:
        return None
    path_format = getattr(context, "path_format", None)
    return path_format if isinstance(path_format, str) else None


# A Mount that matched but whose app found nothing is relative to the level above it
def _route_level(path_regex: Any, mount_prefix: str, relative_path: str) -> str:
    match = getattr(path_regex, "match", None)
    if match is None:
        return mount_prefix

    cut = len(mount_prefix)
    while cut >= 0:
        if match(relative_path[cut:]):
            return mount_prefix[:cut]
        cut = mount_prefix.rfind("/", 0, cut)
    return mount_prefix
