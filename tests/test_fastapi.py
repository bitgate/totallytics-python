from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import totallytics
from totallytics.fastapi import TotallyticsMiddleware

from .conftest import KEY, Ingest, errors_of, metrics_of


def build_app(**options: Any) -> FastAPI:
    app = FastAPI()
    orgs = APIRouter(prefix="/orgs/{org}")

    @orgs.get("/items/{item_id}")
    def item(org: str, item_id: int) -> dict:
        return {}

    @app.get("/users/{user_id}")
    def user(user_id: int) -> dict:
        return {}

    @app.get("/files/{file_path:path}")
    def file(file_path: str) -> dict:
        return {}

    @app.get("/boom")
    def boom() -> dict:
        raise ValueError("boom")

    @app.get("/forbidden")
    def forbidden() -> dict:
        raise HTTPException(403, "no")

    @app.get("/stream")
    def stream() -> StreamingResponse:
        def chunks() -> Any:
            yield b"first"
            time.sleep(0.3)
            yield b"second"

        return StreamingResponse(chunks())

    app.include_router(orgs, prefix="/v1")
    sub = FastAPI()

    @sub.get("/deep/{name}")
    def deep(name: str) -> dict:
        return {}

    app.mount("/sub", sub)
    app.add_middleware(TotallyticsMiddleware, api_key=KEY, **options)
    return app


def routes_of(ingest: Ingest) -> list[tuple[str, str, int]]:
    return [(row["method"], row["route"], row["status"]) for row in metrics_of(ingest)]


def test_reports_route_templates(ingest: Ingest) -> None:
    client = TestClient(build_app())
    client.get("/users/42?token=secret")
    client.get("/files/a/b.txt")
    client.get("/v1/orgs/acme/items/7")
    client.get("/sub/deep/x")
    client.get("/nope/123")
    client.post("/users/42")
    totallytics.flush()

    assert routes_of(ingest) == [
        ("GET", "/files/:file_path", 200),
        ("GET", "/nope/123", 404),
        ("GET", "/sub/deep/:name", 200),
        ("GET", "/users/:user_id", 200),
        ("POST", "/users/:user_id", 405),
        ("GET", "/v1/orgs/:org/items/:item_id", 200),
    ]
    assert ingest.payloads[0]["sdk"].endswith(" fastapi")


def test_samples_errors_without_query_strings(ingest: Ingest) -> None:
    client = TestClient(build_app(), raise_server_exceptions=False)
    assert client.get("/boom?token=secret").status_code == 500
    assert client.get("/forbidden").status_code == 403
    totallytics.flush()

    assert [(row["route"], row["path"], row["status"], row.get("message")) for row in errors_of(ingest)] == [
        ("/boom", "/boom", 500, "ValueError: boom"),
        ("/forbidden", "/forbidden", 403, None),
    ]


def test_excludes_streaming_time(ingest: Ingest) -> None:
    client = TestClient(build_app())
    assert client.get("/stream").content == b"firstsecond"
    totallytics.flush()
    assert metrics_of(ingest)[0]["duration_ms_sum"] < 250


def test_callbacks_receive_the_request(ingest: Ingest) -> None:
    def consumer(request: Request) -> str | None:
        return request.headers.get("x-org")

    client = TestClient(build_app(consumer=consumer, ignore=lambda request: request.url.path.startswith("/files")))
    client.get("/users/1", headers={"x-org": "acme"})
    client.get("/files/secret.txt")
    totallytics.flush()

    (metric,) = metrics_of(ingest)
    assert (metric["route"], metric["consumer"]) == ("/users/:user_id", "acme")


def test_flushes_on_lifespan_shutdown(ingest: Ingest) -> None:
    with TestClient(build_app()) as client:
        client.get("/users/1")
        assert ingest.calls == []
    assert routes_of(ingest) == [("GET", "/users/:user_id", 200)]
