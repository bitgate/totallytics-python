from __future__ import annotations

from typing import Any, NamedTuple

from ._histogram import bucket
from ._util import round_ms

MAX_KEYS = 10_000
MAX_SERVER_ERROR_SAMPLES = 50
MAX_CLIENT_ERROR_SAMPLES = 20

Row = dict[str, Any]


class Measurement(NamedTuple):
    ts: int
    method: str
    route: str
    path: str
    status: int
    duration_ms: float
    user_agent: str | None = None
    consumer: str | None = None
    message: str | None = None


class Aggregator:
    def __init__(self, sample_errors: bool) -> None:
        self.sample_errors = sample_errors
        self._rows: dict[tuple[int, str, str, int, str, str], Row] = {}
        self._server_errors: list[Row] = []
        self._client_errors: list[Row] = []

    @property
    def size(self) -> int:
        return len(self._rows)

    def add(self, measurement: Measurement) -> bool:
        minute = measurement.ts // 60_000 * 60
        key = (
            minute,
            measurement.method,
            measurement.route,
            measurement.status,
            measurement.user_agent or "",
            measurement.consumer or "",
        )

        row = self._rows.get(key)
        if row is None:
            if len(self._rows) >= MAX_KEYS:
                return False
            row = {
                "minute": minute,
                "method": measurement.method,
                "route": measurement.route,
                "status": measurement.status,
                "count": 0,
                "duration_ms_sum": 0,
                "histogram": {},
            }
            if measurement.user_agent:
                row["user_agent"] = measurement.user_agent
            if measurement.consumer:
                row["consumer"] = measurement.consumer
            self._rows[key] = row

        index = bucket(measurement.duration_ms)
        histogram = row["histogram"]
        row["count"] += 1
        row["duration_ms_sum"] += measurement.duration_ms
        histogram[index] = histogram.get(index, 0) + 1

        if self.sample_errors and measurement.status >= 400:
            self._sample(measurement)
        return True

    def drain(self) -> tuple[list[Row], list[Row]]:
        metrics = list(self._rows.values())
        for row in metrics:
            row["duration_ms_sum"] = round_ms(row["duration_ms_sum"])
            row["histogram"] = {str(index): count for index, count in sorted(row["histogram"].items())}
        errors = self._server_errors + self._client_errors

        self._rows = {}
        self._server_errors = []
        self._client_errors = []
        return metrics, errors

    def _sample(self, measurement: Measurement) -> None:
        server_error = measurement.status >= 500
        samples = self._server_errors if server_error else self._client_errors
        if len(samples) >= (MAX_SERVER_ERROR_SAMPLES if server_error else MAX_CLIENT_ERROR_SAMPLES):
            return

        row: Row = {
            "ts": measurement.ts,
            "method": measurement.method,
            "route": measurement.route,
            "path": measurement.path,
            "status": measurement.status,
            "duration_ms": round_ms(measurement.duration_ms),
        }
        if measurement.user_agent:
            row["user_agent"] = measurement.user_agent
        if measurement.consumer:
            row["consumer"] = measurement.consumer
        if measurement.message:
            row["message"] = measurement.message
        samples.append(row)
