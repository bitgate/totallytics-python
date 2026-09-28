from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from totallytics.asgi import TotallyticsMiddleware

from .conftest import KEY, Ingest, errors_of, metrics_of

Message = dict[str, Any]
App = Callable[..., Any]


def http_scope(path: str = "/things/7", method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [(b"user-agent", b"pytest/1")],
    }


def call(app: App, scope: dict, inbox: list[Message] | None = None) -> list[Message]:
    sent: list[Message] = []
    pending = list(inbox or [{"type": "http.request", "body": b"", "more_body": False}])

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def responding(status: int = 200, delay: float = 0.0) -> App:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await asyncio.sleep(delay)
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def test_records_requests_until_the_response_starts(ingest: Ingest) -> None:
    middleware = TotallyticsMiddleware(responding(201, delay=0.3), api_key=KEY)
    sent = call(middleware, http_scope())
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    middleware.flush()

    (metric,) = metrics_of(ingest)
    assert metric["route"] == "/things/7"
    assert metric["status"] == 201
    assert metric["user_agent"] == "pytest/1"
    assert metric["duration_ms_sum"] < 250
    assert ingest.payloads[0]["sdk"].endswith(" asgi")


def test_uses_the_route_template_left_in_scope(ingest: Ingest) -> None:
    route = SimpleNamespace(path_format="/things/{thing_id:int}", path_regex=re.compile(r"^/things/(?P<thing_id>\d+)$"))

    async def app(scope: dict, receive: Any, send: Any) -> None:
        scope["route"] = route
        await responding()(scope, receive, send)

    middleware = TotallyticsMiddleware(app, api_key=KEY)
    call(middleware, http_scope())
    middleware.flush()
    assert metrics_of(ingest)[0]["route"] == "/things/:thing_id"


def test_records_exceptions_as_500_and_reraises(ingest: Ingest) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        raise ValueError("boom")

    middleware = TotallyticsMiddleware(app, api_key=KEY)
    with pytest.raises(ValueError):
        call(middleware, http_scope())
    middleware.flush()

    (error,) = errors_of(ingest)
    assert error["status"] == 500
    assert error["message"] == "ValueError: boom"


def test_records_499_when_the_client_leaves_before_a_response(ingest: Ingest) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass

    middleware = TotallyticsMiddleware(app, api_key=KEY)
    call(middleware, http_scope())
    middleware.flush()
    assert metrics_of(ingest)[0]["status"] == 499


def test_records_499_when_the_request_is_cancelled(ingest: Ingest) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        raise asyncio.CancelledError

    middleware = TotallyticsMiddleware(app, api_key=KEY)
    with pytest.raises(asyncio.CancelledError):
        call(middleware, http_scope())
    middleware.flush()
    assert metrics_of(ingest)[0]["status"] == 499


def test_callbacks_receive_the_scope(ingest: Ingest) -> None:
    def consumer(scope: dict) -> str:
        return dict(scope["headers"])[b"x-org"].decode()

    middleware = TotallyticsMiddleware(
        responding(),
        api_key=lambda: KEY,
        consumer=consumer,
        route=lambda scope: "/custom" if scope["path"].startswith("/custom") else None,
        ignore=lambda scope: scope["path"] == "/health",
    )
    for path in ("/things/7", "/custom/1", "/health"):
        call(middleware, http_scope(path, headers=[(b"x-org", b"acme")]))
    middleware.flush()

    assert [(row["route"], row["consumer"]) for row in metrics_of(ingest)] == [
        ("/custom", "acme"),
        ("/things/7", "acme"),
    ]


def test_failing_callbacks_never_break_requests(ingest: Ingest) -> None:
    def explode(value: object = None) -> str:
        raise RuntimeError("nope")

    middleware = TotallyticsMiddleware(responding(), api_key=KEY, consumer=explode, route=explode, ignore=explode)
    call(middleware, http_scope())
    unkeyed = TotallyticsMiddleware(responding(), api_key=explode)
    call(unkeyed, http_scope())
    middleware.flush()
    unkeyed.flush()

    (metric,) = metrics_of(ingest)
    assert metric["route"] == "/things/7"
    assert "consumer" not in metric


def test_passes_other_scopes_through(ingest: Ingest) -> None:
    seen: list[str] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    middleware = TotallyticsMiddleware(app, api_key=KEY)
    call(middleware, {"type": "websocket", "path": "/ws"})
    middleware.flush()
    assert seen == ["websocket"]
    assert ingest.calls == []


def test_flushes_before_lifespan_shutdown_completes(ingest: Ingest) -> None:
    order: list[str] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            await responding()(scope, receive, send)
            return
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    middleware = TotallyticsMiddleware(app, api_key=KEY)
    call(middleware, http_scope())

    async def run_lifespan() -> None:
        inbox = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

        async def receive() -> Message:
            return inbox.pop(0)

        async def send(message: Message) -> None:
            order.append(f"{message['type']} after {len(ingest.calls)} posts")

        await middleware({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    asyncio.run(run_lifespan())
    assert order == ["lifespan.startup.complete after 0 posts", "lifespan.shutdown.complete after 1 posts"]
