from __future__ import annotations

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

import starlette
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

import totallytics
from totallytics.starlette import TotallyticsMiddleware

from .conftest import KEY, Ingest, metrics_of

# Starlette only leaves the matched route in scope since 1.0
TEMPLATES = int(starlette.__version__.split(".")[0]) >= 1


def ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def test_reports_route_templates(ingest: Ingest) -> None:
    app = Starlette(
        routes=[
            Route("/r/{rid:int}", ok),
            Mount("/m/{tenant}", routes=[Route("/x/{item}", ok)]),
        ]
    )
    app.add_middleware(TotallyticsMiddleware, api_key=KEY, consumer=lambda request: request.path_params.get("tenant"))
    client = TestClient(app)
    client.get("/r/7")
    client.get("/m/acme/x/9")
    client.get("/m/acme/nope")
    totallytics.flush()

    expected = [
        ("/m/:tenant/:path", 404, "acme"),
        ("/m/acme/x/:item", 200, "acme"),
        ("/r/:rid", 200, None),
    ]
    if not TEMPLATES:
        expected = [("/m/acme/nope", 404, "acme"), ("/m/acme/x/9", 200, "acme"), ("/r/7", 200, None)]
    assert [(row["route"], row["status"], row.get("consumer")) for row in metrics_of(ingest)] == expected
    assert ingest.payloads[0]["sdk"].endswith(" starlette")
