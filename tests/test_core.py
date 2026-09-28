from __future__ import annotations

import math

import pytest

from totallytics import bucket
from totallytics._aggregator import MAX_KEYS, Aggregator, Measurement
from totallytics._histogram import _fdlibm_log
from totallytics._util import clip, describe_error, path_of, round_ms

MINUTE_MS = 1_790_424_000_000


def measurement(**overrides: object) -> Measurement:
    fields: dict[str, object] = {
        "ts": MINUTE_MS + 1_000,
        "method": "GET",
        "route": "/users/:id",
        "path": "/users/42",
        "status": 200,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return Measurement(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [
        (-5, 0),
        (0, 0),
        (1, 0),
        (math.nan, 0),
        (1.0000001, 1),
        (1.08, 1),
        (1.09, 2),
        (10, 30),
        (1_000, 90),
        (60_000, 143),
        (1e300, 250),
        (math.inf, 250),
    ],
)
def test_bucket(duration_ms: float, expected: int) -> None:
    assert bucket(duration_ms) == expected


def test_log_is_bit_identical_to_v8_where_libms_disagree() -> None:
    assert _fdlibm_log(5.033833715357254).hex() == "0x1.9dbe1839a9532p+0"
    assert bucket(5.033833715357254) == 21


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.example.com/v1/items?limit=5#top", "/v1/items"),
        ("http://localhost:8080", "/"),
        ("/search?q=a/b", "/search"),
        ("/page#section?x", "/page"),
        ("?only=query", "/"),
        ("", "/"),
    ],
)
def test_path_of_strips_origin_query_and_fragment(url: str, expected: str) -> None:
    assert path_of(url) == expected


def test_clip_counts_utf16_units_without_splitting_characters() -> None:
    assert clip("abcdef", 3) == "abc"
    assert clip("a😀b", 2) == "a"
    assert clip("a😀b", 3) == "a😀"
    assert clip("é" * 10, 4) == "éééé"


def test_describe_error() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert describe_error(ValueError("boom")) == "ValueError: boom"
    assert describe_error(KeyError()) == "KeyError"
    assert describe_error("plain text") == "plain text"
    assert describe_error(None) is None
    assert describe_error(Unprintable()) is None


def test_round_ms_rounds_half_up_like_math_round() -> None:
    assert round_ms(0.0025) == 0.003
    assert round(0.0025 * 1000) / 1000 == 0.002
    assert round_ms(12.3456) == 12.346
    assert round_ms(0.1 + 0.2) == 0.3


def test_aggregator_keys_rows_by_minute_method_route_status_user_agent_and_consumer() -> None:
    aggregator = Aggregator(sample_errors=True)
    for changed in (
        {},
        {"duration_ms": 7.5, "ts": MINUTE_MS + 59_999},
        {"ts": MINUTE_MS + 60_000},
        {"method": "POST"},
        {"route": "/other"},
        {"status": 201},
        {"user_agent": "curl/8"},
        {"consumer": "acme"},
    ):
        assert aggregator.add(measurement(**changed))

    metrics, errors = aggregator.drain()
    assert len(metrics) == 7
    assert errors == []
    assert metrics[0] == {
        "minute": MINUTE_MS // 1000,
        "method": "GET",
        "route": "/users/:id",
        "status": 200,
        "count": 2,
        "duration_ms_sum": 20.0,
        "histogram": {"27": 1, "33": 1},
    }
    assert metrics[1]["minute"] == MINUTE_MS // 1000 + 60
    assert metrics[-2]["user_agent"] == "curl/8"
    assert metrics[-1]["consumer"] == "acme"
    assert aggregator.drain() == ([], [])


def test_histogram_keys_are_sorted_numerically() -> None:
    aggregator = Aggregator(sample_errors=True)
    for duration_ms in (100, 1.5, 3):
        aggregator.add(measurement(duration_ms=duration_ms))

    (row,), _ = aggregator.drain()
    assert list(row["histogram"]) == ["6", "15", "60"]


def test_aggregator_caps_distinct_keys() -> None:
    aggregator = Aggregator(sample_errors=True)
    for index in range(MAX_KEYS):
        assert aggregator.add(measurement(route=f"/r/{index}"))

    assert not aggregator.add(measurement(route="/one-too-many", status=500))
    assert aggregator.add(measurement(route="/r/0"))
    metrics, errors = aggregator.drain()
    assert len(metrics) == MAX_KEYS
    assert errors == []


def test_error_samples_are_capped_and_5xx_come_first() -> None:
    aggregator = Aggregator(sample_errors=True)
    for index in range(30):
        aggregator.add(measurement(status=404, path=f"/c/{index}"))
    for index in range(60):
        aggregator.add(measurement(status=503, path=f"/s/{index}", duration_ms=1.23456, message="boom"))
    aggregator.add(measurement(status=302))

    _, errors = aggregator.drain()
    assert [row["status"] for row in errors] == [503] * 50 + [404] * 20
    assert errors[0] == {
        "ts": MINUTE_MS + 1_000,
        "method": "GET",
        "route": "/users/:id",
        "path": "/s/0",
        "status": 503,
        "duration_ms": 1.235,
        "message": "boom",
    }


def test_error_samples_can_be_disabled() -> None:
    aggregator = Aggregator(sample_errors=False)
    aggregator.add(measurement(status=500))
    metrics, errors = aggregator.drain()
    assert len(metrics) == 1
    assert errors == []
