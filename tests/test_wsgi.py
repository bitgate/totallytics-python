from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Callable
from wsgiref.util import setup_testing_defaults

import pytest

from totallytics.wsgi import TotallyticsMiddleware

from .conftest import KEY, Ingest, errors_of, metrics_of


def environ(path: str = "/things/7", **overrides: str) -> dict:
    values: dict = {"PATH_INFO": path, "HTTP_USER_AGENT": "pytest/1"}
    values.update(overrides)
    setup_testing_defaults(values)
    return values


def call(app: Callable[..., Iterable[bytes]], env: dict) -> tuple[list[str], bytes]:
    statuses: list[str] = []

    def start_response(status: str, headers: list, exc_info: Any = None) -> Callable[[bytes], None]:
        statuses.append(status)
        return lambda data: None

    body = app(env, start_response)
    try:
        return statuses, b"".join(body)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()


def hello(env: dict, start_response: Callable[..., Any]) -> list[bytes]:
    start_response("201 Created", [("Content-Type", "text/plain")])
    return [b"hello"]


def test_records_requests(ingest: Ingest) -> None:
    middleware = TotallyticsMiddleware(hello, api_key=KEY, consumer=lambda env: env.get("HTTP_X_ORG"))
    assert call(middleware, environ(HTTP_X_ORG="acme")) == (["201 Created"], b"hello")
    middleware.flush()

    (metric,) = metrics_of(ingest)
    assert (metric["route"], metric["status"], metric["user_agent"], metric["consumer"]) == (
        "/things/7",
        201,
        "pytest/1",
        "acme",
    )
    assert ingest.payloads[0]["sdk"].endswith(" wsgi")


def test_returns_the_body_untouched_when_the_response_already_started() -> None:
    body = [b"x"]

    def app(env: dict, start_response: Callable[..., Any]) -> list[bytes]:
        start_response("200 OK", [])
        return body

    assert TotallyticsMiddleware(app, api_key=KEY)(environ(), lambda *args: None) is body


def test_records_lazy_responses_and_closes_them(ingest: Ingest) -> None:
    closed: list[bool] = []

    class Body:
        def __init__(self, start_response: Callable[..., Any]) -> None:
            self.start_response = start_response

        def __iter__(self) -> Any:
            self.start_response("404 Not Found", [])
            yield b"missing"

        def close(self) -> None:
            closed.append(True)

    middleware = TotallyticsMiddleware(lambda env, start_response: Body(start_response), api_key=KEY)
    assert call(middleware, environ()) == (["404 Not Found"], b"missing")
    middleware.flush()
    assert closed == [True]
    assert metrics_of(ingest)[0]["status"] == 404


def test_records_exceptions_as_500_and_reraises(ingest: Ingest) -> None:
    def app(env: dict, start_response: Callable[..., Any]) -> list[bytes]:
        raise KeyError("missing")

    middleware = TotallyticsMiddleware(app, api_key=KEY)
    with pytest.raises(KeyError):
        call(middleware, environ())
    middleware.flush()

    (error,) = errors_of(ingest)
    assert (error["status"], error["message"]) == (500, "KeyError: 'missing'")


def test_includes_the_script_name_and_decodes_paths(ingest: Ingest) -> None:
    middleware = TotallyticsMiddleware(hello, api_key=KEY)
    call(middleware, environ("/caf\xc3\xa9", SCRIPT_NAME="/api"))
    middleware.flush()
    assert metrics_of(ingest)[0]["route"] == "/api/café"
