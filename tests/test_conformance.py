"""Replays scenarios recorded from the JS SDK (scripts/generate_fixtures.mts) and expects identical payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from totallytics import Totallytics, bucket

from .conftest import KEY, Ingest

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "conformance.json").read_text(encoding="utf-8"))

FIELDS = {
    "method": "method",
    "path": "path",
    "route": "route",
    "status": "status",
    "durationMs": "duration_ms",
    "startedAt": "started_at",
    "userAgent": "user_agent",
    "consumer": "consumer",
    "error": "error",
}

RESPONDERS = {
    "413 split": lambda payload: 413 if len(payload["metrics"]) + len(payload["errors"]) > 3 else 202,
}


def decode(value: Any) -> Any:
    if isinstance(value, dict) and "$number" in value:
        return float(value["$number"])
    if isinstance(value, dict) and "$error" in value:
        error = value["$error"]
        return type(error["name"], (Exception,), {})(error["message"])
    return value


# JSON drops undefined, which the JS SDK treats like Python's None
def arguments(entry: dict[str, Any]) -> dict[str, Any]:
    required = {"method": None, "path": None, "status": None, "duration_ms": None}
    return {**required, **{FIELDS[field]: decode(value) for field, value in entry.items()}}


def by_rows(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(batches, key=lambda batch: [row["route"] for row in batch["metrics"] + batch["errors"]])


def test_fixture_comes_from_the_js_sdk() -> None:
    assert FIXTURE["source"].startswith("totallytics-js ")
    assert len(FIXTURE["buckets"]) > 2000
    assert len(FIXTURE["scenarios"]) == 9


def test_buckets_match_js() -> None:
    mismatches = [
        (duration, expected, bucket(decode(duration)))
        for duration, expected in FIXTURE["buckets"]
        if bucket(decode(duration)) != expected
    ]
    assert mismatches == []


@pytest.mark.parametrize("scenario", FIXTURE["scenarios"], ids=lambda scenario: scenario["name"])
def test_payloads_match_js(scenario: dict[str, Any], ingest: Ingest) -> None:
    options = scenario["options"]
    client = Totallytics(
        KEY,
        max_batch_rows=options.get("maxBatchRows"),
        error_samples=options.get("errorSamples", True),
    )
    ingest.respond = RESPONDERS.get(scenario["name"])

    for entry in scenario["entries"]:
        client.record(**arguments(entry))
    assert client.flush(5)

    batches = [{"metrics": payload["metrics"], "errors": payload["errors"]} for payload in ingest.accepted]
    if scenario["name"] in RESPONDERS:
        assert by_rows(batches) == by_rows(scenario["batches"])
    else:
        assert batches == scenario["batches"]
