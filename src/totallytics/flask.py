"""Flask extension. Callbacks receive `flask.request` and run inside the request context."""

from __future__ import annotations

from typing import Any

from flask import Flask, has_request_context, request

from ._middleware import ApiKey, join_route, werkzeug_template
from .wsgi import EXCHANGE_KEY, Environ
from .wsgi import TotallyticsMiddleware as _WSGIMiddleware

__all__ = ["TotallyticsMiddleware"]


class TotallyticsMiddleware(_WSGIMiddleware):
    """`TotallyticsMiddleware(app)` or `init_app(app)`; wraps `app.wsgi_app`."""

    integration = "flask"

    def __init__(self, app: Flask | None = None, api_key: ApiKey = None, **options: Any) -> None:
        super().__init__(None, api_key, **options)
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        self.app = app.wsgi_app
        app.wsgi_app = self  # type: ignore[method-assign]
        app.teardown_request(self._teardown)

    # Teardown sees the matched rule and unhandled exceptions, even ones Flask turned into a 500
    def _teardown(self, error: BaseException | None) -> None:
        exchange = request.environ.get(EXCHANGE_KEY)
        if exchange is None or (exchange.status is None and error is None):
            return
        self._finish(request.environ, exchange, error)

    def _callback_target(self, environ: Environ) -> Any:
        return request if has_request_context() else environ

    def _template(self, environ: Environ) -> str | None:
        rule = request.url_rule if has_request_context() else None
        if rule is None:
            return None
        return join_route(request.script_root, werkzeug_template(rule.rule))
