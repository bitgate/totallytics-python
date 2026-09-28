from __future__ import annotations

import re
from typing import Any, Callable, Optional, Union

from ._client import Totallytics
from ._runtime import default_key, normalize_key, warn, warn_no_key
from ._util import attempt, describe_error

ApiKey = Union[str, Callable[[], Optional[str]], None]
Callback = Callable[[Any], Any]

_STARLETTE_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[a-zA-Z_][a-zA-Z0-9_]*)?\}")
_WERKZEUG_PARAM = re.compile(r"<(?:[a-zA-Z_][a-zA-Z0-9_]*(?:\(.*?\))?:)?([a-zA-Z_][a-zA-Z0-9_]*)>")


class Middleware:
    """Option handling shared by every framework adapter."""

    integration = "python"

    def __init__(
        self,
        api_key: ApiKey = None,
        *,
        endpoint: str | None = None,
        consumer: Callback | None = None,
        route: Callback | None = None,
        ignore: Callback | None = None,
        max_batch_rows: int | None = None,
        flush_interval: float | None = None,
        error_samples: bool = True,
        debug: bool = False,
    ) -> None:
        self.api_key = api_key
        self.consumer = consumer
        self.route = route
        self.ignore = ignore
        self.client = Totallytics(
            None if callable(api_key) else api_key,
            endpoint=endpoint,
            max_batch_rows=max_batch_rows,
            flush_interval=flush_interval,
            error_samples=error_samples,
            debug=debug,
            integration=self.integration,
        )

    def flush(self, timeout: float | None = None) -> bool:
        """Sends everything buffered and waits for in-flight batches. Never raises."""
        return self.client.flush(timeout)

    def _track(
        self,
        target: Any,
        *,
        method: str,
        path: str,
        template: str | None,
        status: int,
        duration_ms: float,
        started_at: int,
        user_agent: str | None,
        error: BaseException | None,
    ) -> None:
        try:
            if callable(self.api_key):
                key = normalize_key(attempt(self.api_key, log=self._log))
            else:
                key = normalize_key(self.api_key) or default_key()
            if not key:
                warn_no_key(self.client.debug)
                return
            if self.ignore is not None and attempt(self.ignore, target, log=self._log):
                return

            self.client.api_key = key
            self.client.record(
                method=method,
                path=path,
                route=(self.route is not None and attempt(self.route, target, log=self._log)) or template,
                status=status,
                duration_ms=duration_ms,
                started_at=started_at,
                user_agent=user_agent,
                consumer=attempt(self.consumer, target, log=self._log) if self.consumer is not None else None,
                error=error,
            )
        except Exception as failure:
            self._log(f"tracking failed: {describe_error(failure)}")

    def _log(self, message: str) -> None:
        if self.client.debug:
            warn(message)


def join_route(base: str, template: str) -> str:
    return base if base and template == "/" else base + template


def starlette_template(path_format: str) -> str:
    return _STARLETTE_PARAM.sub(r":\1", path_format)


def werkzeug_template(rule: str) -> str:
    return _WERKZEUG_PARAM.sub(r":\1", rule)
