"""Prints the exact request bodies the SDK sends for the conformance scenarios plus real FastAPI and Flask traffic.

Usage: PYTHONPATH=. python scripts/server_payloads.py > payloads.json
Then:  npx tsx scripts/validate_server.mts <totallytics server checkout> payloads.json
"""

from __future__ import annotations

import json
import sys
import time
import warnings

import totallytics
from tests.conftest import KEY
from tests.test_conformance import FIXTURE, RESPONDERS, arguments
from totallytics import Totallytics, _transport

FIXTURE_MINUTE_MS = 1_790_424_000_000
bodies: list[str] = []


def capture(endpoint: str, api_key: str, body: bytes, timeout: float) -> tuple[int, str]:
    bodies.append(body.decode("ascii"))
    return 202, "{}"


def replay_scenarios() -> None:
    # Fixtures are pinned to one minute in 2026, the server only takes the last 7 days
    shift = time.time_ns() // 1_000_000 // 60_000 * 60_000 - FIXTURE_MINUTE_MS
    for scenario in FIXTURE["scenarios"]:
        if scenario["name"] in RESPONDERS:
            continue
        options = scenario["options"]
        client = Totallytics(
            KEY, max_batch_rows=options.get("maxBatchRows"), error_samples=options.get("errorSamples", True)
        )
        for entry in scenario["entries"]:
            values = arguments(entry)
            if isinstance(values.get("started_at"), (int, float)):
                values["started_at"] += shift
            client.record(**values)
        client.flush()


def record_extremes() -> None:
    client = Totallytics(KEY, max_batch_rows=5_000)
    for index in range(5_000):
        client.record(
            method=("GET", "POST", "DELETE", "PATCH")[index % 4],
            path=f"/extreme/{index}/😀" + "x" * 600,
            route=f"/extreme/{index}/" + "😀" * 300 if index % 2 else None,
            status=(200, 201, 204, 301, 404, 429, 499, 500, 503)[index % 9],
            duration_ms=index * 7.31 + 0.001,
            user_agent="Mozilla/5.0 " + "é" * 600,
            consumer="😀" * 100,
            error=RuntimeError("m" * 2_000),
        )
    client.flush()


def replay_frameworks() -> None:
    from fastapi.testclient import TestClient

    from tests.test_fastapi import build_app as build_fastapi
    from tests.test_flask import build_app as build_flask

    fastapi_client = TestClient(build_fastapi(), raise_server_exceptions=False)
    flask_client = build_flask()[0].test_client()
    for client in (fastapi_client, flask_client):
        for path in ("/users/1?token=x", "/files/a/b.txt", "/v1/orgs/acme/items/7", "/boom", "/nope/😀", "/stream"):
            client.get(path, headers={"User-Agent": "curl/8.4.0"})
        client.post("/users/1")
    totallytics.flush()


if __name__ == "__main__":
    warnings.simplefilter("ignore")
    _transport.post = capture
    replay_scenarios()
    record_extremes()
    replay_frameworks()
    json.dump(bodies, sys.stdout)
