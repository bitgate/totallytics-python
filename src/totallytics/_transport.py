from __future__ import annotations

import http.client
import json
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, NamedTuple

from ._runtime import warn, warn_once
from ._util import describe_error
from ._version import __version__

USER_AGENT = f"totallytics-python/{__version__}"

MAX_ATTEMPTS = 3
MAX_PENDING = 20
TIMEOUT_SECONDS = 10.0
BACKOFF_BASE_SECONDS = 1.0
MAX_RESPONSE_BYTES = 64 * 1024

NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)


class Batch(NamedTuple):
    id: str
    api_key: str
    body: bytes


class _Delivery:
    __slots__ = ("attempts", "batch", "dropped", "due")

    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        self.attempts = 0
        self.due = time.monotonic()
        self.dropped = False


def create_batch(api_key: str, sdk: str | None, metrics: list[Any], errors: list[Any]) -> Batch:
    batch_id = uuid.uuid4().hex
    payload = {"v": 1, "batch_id": batch_id, "sdk": sdk, "metrics": metrics, "errors": errors}
    return Batch(batch_id, api_key, json.dumps(payload, separators=(",", ":")).encode("ascii"))


def halve(batch: Batch) -> tuple[Batch, Batch] | None:
    payload = json.loads(batch.body)
    metrics, errors = payload["metrics"], payload["errors"]
    if len(metrics) + len(errors) < 2:
        return None

    metrics_cut = (len(metrics) + 1) // 2
    errors_cut = len(errors) // 2
    sdk = payload.get("sdk")
    return (
        create_batch(batch.api_key, sdk, metrics[:metrics_cut], errors[:errors_cut]),
        create_batch(batch.api_key, sdk, metrics[metrics_cut:], errors[errors_cut:]),
    )


def backoff(attempt: int) -> float:
    base = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
    return base / 2 + random.random() * base


def post(endpoint: str, api_key: str, body: bytes, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_text(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, _read_text(error)
        finally:
            error.close()


def _read_text(stream: Any) -> str:
    try:
        return stream.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
    except NETWORK_ERRORS:
        return ""


class Transport:
    """Delivers sealed batches: retries with backoff, splits on 413, keeps at most 20 pending."""

    def __init__(self, endpoint: str, debug: bool) -> None:
        self.endpoint = endpoint
        self.debug = debug
        self._pending: list[_Delivery] = []
        self._lock = threading.Lock()
        self._sending = threading.Lock()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def next_due(self) -> float | None:
        with self._lock:
            return min((delivery.due for delivery in self._pending), default=None)

    def enqueue(self, batch: Batch) -> None:
        with self._lock:
            self._pending.append(_Delivery(batch))
            while len(self._pending) > MAX_PENDING:
                oldest = self._pending.pop(0)
                oldest.dropped = True
                self._log(f"dropped batch {oldest.batch.id}: more than {MAX_PENDING} batches pending")

    def pump(self, deadline: float | None = None, wait: bool = False) -> bool:
        """Sends every due batch, and with `wait` also sits out retry backoffs. True once nothing is pending."""
        lock_timeout = -1 if deadline is None else max(deadline - time.monotonic(), 0.0)
        if not self._sending.acquire(timeout=lock_timeout):
            return False

        try:
            while True:
                with self._lock:
                    if not self._pending:
                        return True
                    delivery = min(self._pending, key=_due)

                now = time.monotonic()
                if delivery.due > now:
                    if not wait or (deadline is not None and delivery.due >= deadline):
                        return False
                    time.sleep(delivery.due - now)
                    continue

                timeout = TIMEOUT_SECONDS if deadline is None else min(TIMEOUT_SECONDS, deadline - now)
                if timeout <= 0:
                    return False
                self._attempt(delivery, timeout)
        finally:
            self._sending.release()

    def reset_after_fork(self) -> None:
        self._pending = []
        self._lock = threading.Lock()
        self._sending = threading.Lock()

    # Every attempt sends the same serialized body, so the server can dedup on batch_id
    def _attempt(self, delivery: _Delivery, timeout: float) -> None:
        outcome = self._send(delivery.batch, timeout)
        delivery.attempts += 1

        with self._lock:
            if delivery.dropped:
                return
            if outcome == "retry" and delivery.attempts < MAX_ATTEMPTS:
                delivery.due = time.monotonic() + backoff(delivery.attempts)
                return
            self._pending.remove(delivery)

        if outcome == "retry":
            self._log(f"dropped batch {delivery.batch.id} after {MAX_ATTEMPTS} attempts")
        elif outcome == "split":
            self._split(delivery.batch)

    def _split(self, batch: Batch) -> None:
        halves = halve(batch)
        if halves is None:
            self._log(f"dropped batch {batch.id}: too large and cannot be split")
            return
        for half in halves:
            self.enqueue(half)

    def _send(self, batch: Batch, timeout: float) -> str:
        # Anything raised here counts as a failed attempt, so a batch can never get stuck
        try:
            status, text = post(self.endpoint, batch.api_key, batch.body, timeout)
        except Exception as error:
            self._log(f"batch {batch.id} failed: {describe_error(error)}")
            return "retry"

        if 200 <= status < 300:
            self._report_rejected(batch, text)
            return "done"
        if status == 401:
            warn_once(
                "unauthorized",
                "ingest rejected the API key (401), analytics are being dropped. Check TOTALLYTICS_API_KEY.",
            )
            return "drop"
        if status == 413:
            return "split"

        retry = status in (408, 429) or status >= 500
        self._log(f"batch {batch.id} got HTTP {status}, {'retrying' if retry else 'dropping'}: {text[:200]}")
        return "retry" if retry else "drop"

    def _report_rejected(self, batch: Batch, text: str) -> None:
        if not self.debug:
            return
        try:
            payload = json.loads(text)
        except ValueError:
            return
        rejected = payload.get("rejected") if isinstance(payload, dict) else None
        if isinstance(rejected, int) and not isinstance(rejected, bool) and rejected > 0:
            self._log(f"batch {batch.id}: server rejected {rejected} rows")

    def _log(self, message: str) -> None:
        if self.debug:
            warn(message)


def _due(delivery: _Delivery) -> float:
    return delivery.due
