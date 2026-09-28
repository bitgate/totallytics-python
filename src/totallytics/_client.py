from __future__ import annotations

import atexit
import math
import os
import threading
import time
import weakref

from . import _runtime
from ._aggregator import Aggregator, Measurement
from ._runtime import default_key, normalize_key, warn, warn_no_key
from ._transport import USER_AGENT, Transport, create_batch
from ._util import clip, describe_error, path_of

DEFAULT_ENDPOINT = "https://totallytics.com/api/ingest"

MAX_SDK = 64
MAX_PATH = 512
MAX_USER_AGENT = 512
MAX_CONSUMER = 128
MAX_MESSAGE = 1000
SHUTDOWN_BUDGET_SECONDS = 5.0

_clients: weakref.WeakSet[Totallytics] = weakref.WeakSet()


class Totallytics:
    """Aggregates finished requests per minute and ships them to Totallytics from one background thread."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        endpoint: str | None = None,
        max_batch_rows: int | None = None,
        flush_interval: float | None = None,
        error_samples: bool = True,
        debug: bool = False,
        integration: str | None = None,
    ) -> None:
        self.api_key = normalize_key(api_key) or default_key()
        self.sdk = clip(f"{USER_AGENT} {integration}" if integration else USER_AGENT, MAX_SDK)
        self.debug = bool(debug)
        self.max_batch_rows = int(_clamp(max_batch_rows, 1, 5_000, 1_000, integer=True))
        self.flush_interval = _clamp(flush_interval, 0.1, 3_600.0, 10.0)

        self._aggregator = Aggregator(error_samples is not False)
        self._transport = Transport(endpoint or DEFAULT_ENDPOINT, self.debug)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._worker: threading.Thread | None = None
        self._generation = 0
        self._full = False
        _clients.add(self)

    def record(
        self,
        *,
        method: str | None,
        path: str | None,
        status: int,
        duration_ms: float,
        route: str | None = None,
        started_at: float | None = None,
        user_agent: str | None = None,
        consumer: object = None,
        error: object = None,
    ) -> None:
        """Records one finished request. `started_at` is unix milliseconds. Never raises."""
        try:
            if not self.api_key:
                warn_no_key(self.debug)
                return

            status_code = _status_code(status)
            if status_code is None:
                return

            duration = float(duration_ms) if _is_finite(duration_ms) and duration_ms > 0 else 0.0
            now_ms = time.time_ns() // 1_000_000
            request_path = clip(path_of(str(path or "/")), MAX_PATH)
            message = describe_error(error)
            measurement = Measurement(
                ts=math.floor(started_at if _is_finite(started_at) else now_ms - duration),
                method=str(method or "GET").upper(),
                route=clip(str(route), MAX_PATH) if route else request_path,
                path=request_path,
                status=status_code,
                duration_ms=duration,
                user_agent=clip(str(user_agent), MAX_USER_AGENT) if user_agent else None,
                consumer=clip(str(consumer), MAX_CONSUMER) if consumer is not None and consumer != "" else None,
                message=clip(message, MAX_MESSAGE) if message else None,
            )

            with self._lock:
                if not self._aggregator.add(measurement):
                    return
                if self._aggregator.size >= self.max_batch_rows:
                    self._full = True
                    self._wake.notify_all()
                if self._worker is None:
                    self._start_worker()
        except Exception as failure:
            self._log(f"record failed: {describe_error(failure)}")

    def flush(self, timeout: float | None = None) -> bool:
        """Sends everything buffered and waits for in-flight batches. False when `timeout` ran out. Never raises."""
        try:
            deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0.0)
            self._flush_buffer()
            delivered = self._transport.pump(deadline, wait=True)
            if not delivered:
                with self._lock:
                    self._wake.notify_all()
            return delivered
        except Exception as failure:
            self._log(f"flush failed: {describe_error(failure)}")
            return False

    def shutdown(self, timeout: float | None = SHUTDOWN_BUDGET_SECONDS) -> bool:
        """Flushes, then stops the background thread. Recording again starts a new one."""
        delivered = self.flush(timeout)
        with self._lock:
            worker, self._worker = self._worker, None
            self._generation += 1
            self._wake.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(1.0)
        return delivered

    def _start_worker(self) -> None:
        worker = threading.Thread(target=self._work, args=(self._generation,), name="totallytics", daemon=True)
        worker.start()
        self._worker = worker

    def _work(self, generation: int) -> None:
        next_flush = time.monotonic() + self.flush_interval
        while True:
            try:
                with self._lock:
                    while generation == self._generation and not self._full:
                        next_due = self._transport.next_due()
                        wake_at = next_flush if next_due is None else min(next_flush, next_due)
                        remaining = wake_at - time.monotonic()
                        if remaining <= 0:
                            break
                        self._wake.wait(remaining)
                    if generation != self._generation:
                        return

                now = time.monotonic()
                if self._full or now >= next_flush:
                    next_flush = now + self.flush_interval
                    self._flush_buffer()
                self._transport.pump()
            except Exception as failure:
                self._log(f"background flush failed: {describe_error(failure)}")
                time.sleep(1.0)

    def _flush_buffer(self) -> None:
        try:
            with self._lock:
                self._full = False
                api_key = self.api_key
                if not api_key or self._aggregator.size == 0:
                    return
                metrics, errors = self._aggregator.drain()

            for start in range(0, len(metrics), self.max_batch_rows):
                rows = metrics[start : start + self.max_batch_rows]
                self._transport.enqueue(create_batch(api_key, self.sdk, rows, errors if start == 0 else []))
        except Exception as failure:
            self._log(f"flush failed: {describe_error(failure)}")

    def _reset_after_fork(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._worker = None
        self._full = False
        self._aggregator = Aggregator(self._aggregator.sample_errors)
        self._transport.reset_after_fork()

    def _log(self, message: str) -> None:
        if self.debug:
            warn(message)


def flush(timeout: float | None = None) -> bool:
    """Flushes every live client, e.g. at the end of a serverless invocation. Never raises."""
    deadline = None if timeout is None else time.monotonic() + timeout
    delivered = True
    for client in list(_clients):
        remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
        delivered = client.flush(remaining) and delivered
    return delivered


def _status_code(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    status = int(value)
    return status if 100 <= status <= 599 else None


def _is_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(value: object, low: float, high: float, fallback: float, integer: bool = False) -> float:
    if not _is_finite(value):
        return fallback
    number = math.floor(value) if integer else float(value)
    return min(max(number, low), high)


# Each process ships only what it recorded itself, so a forked child starts clean
def _reset_after_fork() -> None:
    _runtime.reset_after_fork()
    for client in list(_clients):
        client._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)

atexit.register(flush, SHUTDOWN_BUDGET_SECONDS)
