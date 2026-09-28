from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("flask")

from flask import Blueprint, Flask, Request, Response, abort

from totallytics.flask import TotallyticsMiddleware

from .conftest import KEY, Ingest, errors_of, metrics_of


class Teapot(Exception):
    pass


def build_app(testing: bool = False, **options: Any) -> tuple[Flask, TotallyticsMiddleware]:
    app = Flask(__name__)
    app.testing = testing
    orgs = Blueprint("orgs", __name__, url_prefix="/v1/orgs/<org>")

    @orgs.get("/items/<int:item_id>")
    def item(org: str, item_id: int) -> str:
        return "ok"

    @app.get("/users/<int:user_id>")
    def user(user_id: int) -> str:
        return "ok"

    @app.get("/files/<path:file_path>")
    def file(file_path: str) -> str:
        return "ok"

    @app.get("/boom")
    def boom() -> str:
        raise ValueError("boom")

    @app.get("/forbidden/<name>")
    def forbidden(name: str) -> str:
        abort(403)

    @app.get("/teapot")
    def teapot() -> str:
        raise Teapot("short and stout")

    @app.errorhandler(Teapot)
    def handle_teapot(error: Teapot) -> tuple[str, int]:
        return "tea", 418

    @app.get("/stream")
    def stream() -> Response:
        def chunks() -> Any:
            yield "first"
            time.sleep(0.3)
            yield "second"

        return Response(chunks())

    app.register_blueprint(orgs)
    extension = TotallyticsMiddleware(api_key=KEY, **options)
    extension.init_app(app)
    return app, extension


def test_reports_rule_templates(ingest: Ingest) -> None:
    app, extension = build_app()
    client = app.test_client()
    client.get("/users/42?token=secret")
    client.get("/files/a/b.txt")
    client.get("/v1/orgs/acme/items/7")
    client.get("/nope/123")
    client.post("/users/42")
    client.get("/users/9", environ_overrides={"SCRIPT_NAME": "/api"})
    extension.flush()

    assert [(row["method"], row["route"], row["status"]) for row in metrics_of(ingest)] == [
        ("GET", "/api/users/:user_id", 200),
        ("GET", "/files/:file_path", 200),
        ("GET", "/nope/123", 404),
        ("POST", "/users/42", 405),
        ("GET", "/users/:user_id", 200),
        ("GET", "/v1/orgs/:org/items/:item_id", 200),
    ]
    assert ingest.payloads[0]["sdk"].endswith(" flask")


def test_samples_unhandled_exceptions(ingest: Ingest) -> None:
    app, extension = build_app()
    client = app.test_client()
    assert client.get("/boom?token=secret").status_code == 500
    assert client.get("/forbidden/x").status_code == 403
    assert client.get("/teapot").status_code == 418
    extension.flush()

    assert [(row["route"], row["path"], row["status"], row.get("message")) for row in errors_of(ingest)] == [
        ("/boom", "/boom", 500, "ValueError: boom"),
        ("/forbidden/:name", "/forbidden/x", 403, None),
        ("/teapot", "/teapot", 418, None),
    ]


def test_records_propagated_exceptions_once(ingest: Ingest) -> None:
    app, extension = build_app(testing=True)
    with pytest.raises(ValueError):
        app.test_client().get("/boom")
    extension.flush()

    (error,) = errors_of(ingest)
    assert (error["route"], error["status"], error["message"]) == ("/boom", 500, "ValueError: boom")
    assert metrics_of(ingest)[0]["count"] == 1


def test_excludes_streaming_time(ingest: Ingest) -> None:
    app, extension = build_app()
    assert app.test_client().get("/stream").data == b"firstsecond"
    extension.flush()
    assert metrics_of(ingest)[0]["duration_ms_sum"] < 250


def test_callbacks_receive_the_request(ingest: Ingest) -> None:
    def consumer(request: Request) -> str | None:
        return request.headers.get("X-Org")

    app, extension = build_app(consumer=consumer, ignore=lambda request: request.path.startswith("/files"))
    client = app.test_client()
    client.get("/users/1", headers={"X-Org": "acme"})
    client.get("/files/secret.txt")
    extension.flush()

    (metric,) = metrics_of(ingest)
    assert (metric["route"], metric["consumer"]) == ("/users/:user_id", "acme")


def test_wraps_the_app_directly(ingest: Ingest) -> None:
    app = Flask(__name__)
    app.add_url_rule("/ping", "ping", lambda: "pong")
    extension = TotallyticsMiddleware(app, api_key=KEY)
    app.test_client().get("/ping")
    extension.flush()
    assert metrics_of(ingest)[0]["route"] == "/ping"
