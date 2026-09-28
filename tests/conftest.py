from __future__ import annotations

import json
import time
import weakref
from typing import Any, Callable, NamedTuple

import pytest

from totallytics import Totallytics, _client, _runtime, _transport

KEY = "tt_" + "ab" * 24


class Call(NamedTuple):
    endpoint: str
    api_key: str
    body: bytes
    timeout: float
    status: int | None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.body)


class Ingest:
    """Stands in for the ingest endpoint: keeps every POST and answers with scripted statuses."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.statuses: list[int | Exception] = []
        self.respond: Callable[[dict[str, Any]], int] | None = None

    def __call__(self, endpoint: str, api_key: str, body: bytes, timeout: float) -> tuple[int, str]:
        if self.respond is not None:
            outcome: int | Exception = self.respond(json.loads(body))
        else:
            outcome = self.statuses.pop(0) if self.statuses else 202

        self.calls.append(Call(endpoint, api_key, body, timeout, None if isinstance(outcome, Exception) else outcome))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, '{"accepted":{"metrics":1,"errors":0},"rejected":0}'

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [call.payload for call in self.calls]

    @property
    def accepted(self) -> list[dict[str, Any]]:
        return [call.payload for call in self.calls if call.status is not None and 200 <= call.status < 300]


@pytest.fixture
def ingest(monkeypatch: pytest.MonkeyPatch) -> Ingest:
    fake = Ingest()
    monkeypatch.setattr(_transport, "post", fake)
    return fake


# Every test gets the fake endpoint, so nothing can reach the real one
@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, ingest: Ingest) -> None:
    monkeypatch.setattr(_transport, "backoff", lambda attempt: 0.0)
    monkeypatch.delenv("TOTALLYTICS_API_KEY", raising=False)
    monkeypatch.setattr(_runtime, "_warned", set())
    monkeypatch.setattr(_client, "_clients", weakref.WeakSet())

    # We keep the background thread out of tests unless they are about it
    if request.node.get_closest_marker("worker") is None:
        monkeypatch.setattr(Totallytics, "_start_worker", lambda self: None)


def record(client: Totallytics, **overrides: Any) -> None:
    entry: dict[str, Any] = {
        "method": "GET",
        "path": "/users/42",
        "route": "/users/:id",
        "status": 200,
        "duration_ms": 12.5,
        "started_at": 1_790_424_000_000,
    }
    entry.update(overrides)
    client.record(**entry)


def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def metrics_of(ingest: Ingest) -> list[dict[str, Any]]:
    return sorted(
        (row for payload in ingest.accepted for row in payload["metrics"]),
        key=lambda row: (row["route"], row["method"], row["status"]),
    )


def errors_of(ingest: Ingest) -> list[dict[str, Any]]:
    return [row for payload in ingest.accepted for row in payload["errors"]]
