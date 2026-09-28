"""Generic WSGI middleware. Callbacks receive the WSGI `environ`."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from typing import Any, Callable

from ._middleware import ApiKey, Middleware
from ._util import describe_error

Environ = dict
StartResponse = Callable[..., Any]
WSGIApp = Callable[[Environ, StartResponse], Iterable[bytes]]

EXCHANGE_KEY = "totallytics.exchange"

__all__ = ["TotallyticsMiddleware"]


class _Exchange:
    __slots__ = ("duration_ms", "path", "recorded", "start", "started_at", "status")

    def __init__(self, environ: Environ) -> None:
        self.started_at = time.time_ns() // 1_000_000
        self.start = time.perf_counter()
        self.status: int | None = None
        self.duration_ms: float | None = None
        self.recorded = False
        self.path = _request_path(environ)

    def respond(self, status_line: str) -> None:
        self.status = _status_of(status_line)
        self.duration_ms = self.elapsed_ms()

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000


class TotallyticsMiddleware(Middleware):
    """Records every request. Duration runs until `start_response`, so streamed bodies are excluded."""

    integration = "wsgi"

    def __init__(self, app: WSGIApp | None, api_key: ApiKey = None, **options: Any) -> None:
        super().__init__(api_key, **options)
        self.app = app

    def __call__(self, environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        exchange = _Exchange(environ)
        environ[EXCHANGE_KEY] = exchange

        def watch_start_response(status: str, headers: Any, exc_info: Any = None) -> Any:
            exchange.respond(status)
            return start_response(status, headers, exc_info)

        try:
            body = self.app(environ, watch_start_response)
        except Exception as error:
            self._finish(environ, exchange, error)
            raise

        if exchange.status is not None:
            self._finish(environ, exchange, None)
            return body
        return _WatchedBody(body, lambda error: self._finish(environ, exchange, error))

    def _finish(self, environ: Environ, exchange: _Exchange, error: BaseException | None) -> None:
        if exchange.recorded:
            return
        exchange.recorded = True
        try:
            self._track(
                self._callback_target(environ),
                method=environ.get("REQUEST_METHOD") or "GET",
                path=exchange.path,
                template=self._template(environ),
                status=exchange.status or 500,
                duration_ms=exchange.duration_ms if exchange.duration_ms is not None else exchange.elapsed_ms(),
                started_at=exchange.started_at,
                user_agent=environ.get("HTTP_USER_AGENT"),
                error=error,
            )
        except Exception as failure:
            self._log(f"tracking failed: {describe_error(failure)}")

    def _callback_target(self, environ: Environ) -> Any:
        return environ

    def _template(self, environ: Environ) -> str | None:
        return None


# Apps may call start_response lazily, while the server iterates the body
class _WatchedBody:
    def __init__(self, body: Iterable[bytes], finish: Callable[[BaseException | None], None]) -> None:
        self._body = body
        self._finish = finish

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._body:
                self._finish(None)
                yield chunk
        except Exception as error:
            self._finish(error)
            raise

    def close(self) -> None:
        try:
            close = getattr(self._body, "close", None)
            if close is not None:
                close()
        finally:
            self._finish(None)


def _request_path(environ: Environ) -> str:
    path = (environ.get("SCRIPT_NAME") or "") + (environ.get("PATH_INFO") or "")
    try:
        return path.encode("latin-1").decode("utf-8") or "/"
    except UnicodeError:
        return path or "/"


def _status_of(status_line: str) -> int | None:
    try:
        return int(str(status_line)[:3])
    except ValueError:
        return None
