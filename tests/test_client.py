from __future__ import annotations

import logging
import math
import re
import time
import urllib.error

import pytest

import totallytics
from totallytics import DEFAULT_ENDPOINT, Totallytics, __version__, _client
from totallytics._transport import MAX_ATTEMPTS, MAX_PENDING

from .conftest import KEY, Ingest, errors_of, metrics_of, record


def test_sends_the_wire_payload(ingest: Ingest) -> None:
    client = Totallytics(KEY)
    record(client, user_agent="curl/8.4", consumer="acme")
    assert client.flush()

    (call,) = ingest.calls
    assert call.endpoint == DEFAULT_ENDPOINT
    assert call.api_key == KEY
    assert call.timeout == 10.0
    assert re.fullmatch(r"[0-9a-f]{32}", call.payload["batch_id"])
    assert call.payload == {
        "v": 1,
        "batch_id": call.payload["batch_id"],
        "sdk": f"totallytics-python/{__version__}",
        "metrics": [
            {
                "minute": 1_790_424_000,
                "method": "GET",
                "route": "/users/:id",
                "status": 200,
                "count": 1,
                "duration_ms_sum": 12.5,
                "histogram": {"33": 1},
                "user_agent": "curl/8.4",
                "consumer": "acme",
            }
        ],
        "errors": [],
    }


def test_reads_the_key_from_the_environment(ingest: Ingest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOTALLYTICS_API_KEY", f"  {KEY}  ")
    client = Totallytics()
    record(client)
    client.flush()
    assert [call.api_key for call in ingest.calls] == [KEY]


def test_does_nothing_without_a_key(ingest: Ingest, caplog: pytest.LogCaptureFixture) -> None:
    client = Totallytics("   ")
    record(client)
    assert client.flush()
    assert ingest.calls == []
    assert caplog.records == []


def test_debug_explains_a_missing_key_once(caplog: pytest.LogCaptureFixture) -> None:
    client = Totallytics(debug=True)
    record(client)
    record(client)
    assert [record.getMessage() for record in caplog.records] == [
        "[totallytics] no API key (set TOTALLYTICS_API_KEY or pass api_key), requests are not recorded"
    ]


def test_falls_back_to_the_raw_path_and_clips_fields(ingest: Ingest) -> None:
    client = Totallytics(KEY)
    record(
        client,
        route=None,
        path="https://api.example.com/" + "a" * 600 + "?token=secret",
        status=500,
        user_agent="u" * 600,
        consumer="c" * 200,
        error="e" * 1500,
    )
    client.flush()

    (metric,) = metrics_of(ingest)
    (error,) = errors_of(ingest)
    assert metric["route"] == "/" + "a" * 511
    assert error["path"] == metric["route"]
    assert len(metric["user_agent"]) == 512
    assert len(metric["consumer"]) == 128
    assert len(error["message"]) == 1000


@pytest.mark.parametrize("status", [99, 600, 200.5, True, "200", None, math.nan])
def test_ignores_invalid_statuses(ingest: Ingest, status: object) -> None:
    client = Totallytics(KEY)
    record(client, status=status)
    client.flush()
    assert ingest.calls == []


@pytest.mark.parametrize("duration_ms", [-5, 0, math.nan, math.inf, None, "12"])
def test_records_unusable_durations_as_zero(ingest: Ingest, duration_ms: object) -> None:
    client = Totallytics(KEY)
    record(client, duration_ms=duration_ms)
    client.flush()

    (metric,) = metrics_of(ingest)
    assert metric["duration_ms_sum"] == 0
    assert metric["histogram"] == {"0": 1}


def test_never_raises_on_garbage_input(ingest: Ingest) -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no")

    client = Totallytics(KEY)
    record(client, path=Unprintable())
    record(client, consumer=Unprintable())
    record(client, method=Unprintable())
    record(client, started_at=math.inf, route="/ok")
    assert client.flush()
    assert [row["route"] for row in metrics_of(ingest)] == ["/ok"]


def test_splits_large_buffers_by_max_batch_rows(ingest: Ingest) -> None:
    client = Totallytics(KEY)
    for index in range(2_500):
        record(client, route=f"/r/{index}", status=500 if index < 3 else 200)
    client.flush()

    assert [len(payload["metrics"]) for payload in ingest.payloads] == [1_000, 1_000, 500]
    assert [len(payload["errors"]) for payload in ingest.payloads] == [3, 0, 0]
    assert len({payload["batch_id"] for payload in ingest.payloads}) == 3


@pytest.mark.parametrize(
    ("given", "rows", "interval"),
    [(None, 1_000, 10.0), (0, 1, 0.1), (10_000, 5_000, 3_600.0), (2.7, 2, 2.7), ("x", 1_000, 10.0)],
)
def test_clamps_options(given: object, rows: int, interval: float) -> None:
    client = Totallytics(KEY, max_batch_rows=given, flush_interval=given)  # type: ignore[arg-type]
    assert client.max_batch_rows == rows
    assert client.flush_interval == interval


def test_sdk_names_the_integration() -> None:
    assert Totallytics(KEY, integration="fastapi").sdk == f"totallytics-python/{__version__} fastapi"


def test_module_flush_covers_every_client(ingest: Ingest) -> None:
    first, second = Totallytics(KEY), Totallytics(KEY)
    record(first)
    record(second)
    assert totallytics.flush(5)
    assert len(ingest.calls) == 2


def test_forked_children_start_with_an_empty_buffer(ingest: Ingest) -> None:
    client = Totallytics(KEY)
    record(client)
    _client._reset_after_fork()
    client.flush()
    assert ingest.calls == []


def test_retries_send_the_identical_body(ingest: Ingest) -> None:
    ingest.statuses = [503, urllib.error.URLError("down"), 202]
    client = Totallytics(KEY)
    record(client)
    assert client.flush()

    assert len(ingest.calls) == 3
    assert len({call.body for call in ingest.calls}) == 1


@pytest.mark.parametrize("outcome", [408, 429, 500, 503, TimeoutError(), RuntimeError("bug")])
def test_gives_up_after_three_attempts(ingest: Ingest, outcome: object) -> None:
    ingest.respond = lambda payload: outcome  # type: ignore[assignment, return-value]
    client = Totallytics(KEY)
    record(client)
    assert client.flush()
    assert len(ingest.calls) == MAX_ATTEMPTS


@pytest.mark.parametrize("status", [400, 404, 422])
def test_drops_rejected_batches(ingest: Ingest, status: int) -> None:
    ingest.statuses = [status]
    client = Totallytics(KEY)
    record(client)
    assert client.flush()
    assert len(ingest.calls) == 1


def test_warns_about_401_once_per_process(ingest: Ingest, caplog: pytest.LogCaptureFixture) -> None:
    ingest.respond = lambda payload: 401
    for _ in range(2):
        client = Totallytics(KEY)
        record(client)
        client.flush()

    assert len(ingest.calls) == 2
    warnings = [entry for entry in caplog.records if entry.levelno == logging.WARNING]
    assert [entry.getMessage() for entry in warnings] == [
        "[totallytics] ingest rejected the API key (401), analytics are being dropped. Check TOTALLYTICS_API_KEY."
    ]


def test_splits_413_batches_into_new_batches(ingest: Ingest) -> None:
    ingest.respond = lambda payload: 413 if len(payload["metrics"]) + len(payload["errors"]) > 2 else 202
    client = Totallytics(KEY)
    for index in range(5):
        record(client, route=f"/r/{index}", status=500 if index < 3 else 200)
    client.flush()

    original = ingest.payloads[0]["batch_id"]
    accepted = ingest.accepted
    assert sorted(row["route"] for row in metrics_of(ingest)) == [f"/r/{index}" for index in range(5)]
    assert len(errors_of(ingest)) == 3
    assert all(len(payload["metrics"]) + len(payload["errors"]) <= 2 for payload in accepted)
    batch_ids = [payload["batch_id"] for payload in ingest.payloads]
    assert len(set(batch_ids)) == len(batch_ids)
    assert original not in {payload["batch_id"] for payload in accepted}


def test_drops_a_413_batch_that_cannot_be_split(ingest: Ingest) -> None:
    ingest.respond = lambda payload: 413
    client = Totallytics(KEY)
    record(client)
    assert client.flush()
    assert len(ingest.calls) == 1


def test_keeps_at_most_20_pending_batches(ingest: Ingest) -> None:
    client = Totallytics(KEY)
    for index in range(MAX_PENDING + 1):
        record(client, route=f"/r/{index}")
        client._flush_buffer()
    client.flush()

    assert [payload["metrics"][0]["route"] for payload in ingest.payloads] == [
        f"/r/{index}" for index in range(1, MAX_PENDING + 1)
    ]


def test_flush_respects_its_time_budget(ingest: Ingest) -> None:
    def slow(payload: object) -> int:
        time.sleep(0.2)
        return 503

    ingest.respond = slow
    client = Totallytics(KEY)
    record(client)

    started = time.monotonic()
    assert client.flush(0.1) is False
    assert time.monotonic() - started < 1
    assert ingest.calls[0].timeout <= 0.1
