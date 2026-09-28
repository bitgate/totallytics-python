from __future__ import annotations

import pytest

from totallytics import Totallytics

from .conftest import KEY, Ingest, record, wait_for

pytestmark = pytest.mark.worker


def test_flushes_on_the_interval(ingest: Ingest) -> None:
    client = Totallytics(KEY, flush_interval=0.1)
    record(client)
    assert wait_for(lambda: len(ingest.calls) == 1)
    client.shutdown()


def test_flushes_early_when_the_buffer_is_full(ingest: Ingest) -> None:
    client = Totallytics(KEY, max_batch_rows=5, flush_interval=3_600)
    for index in range(5):
        record(client, route=f"/r/{index}")

    assert wait_for(lambda: len(ingest.calls) == 1)
    assert len(ingest.payloads[0]["metrics"]) == 5
    client.shutdown()


def test_retries_in_the_background(ingest: Ingest) -> None:
    ingest.statuses = [503, 503]
    client = Totallytics(KEY, flush_interval=0.1)
    record(client)
    assert wait_for(lambda: len(ingest.accepted) == 1)
    assert len(ingest.calls) == 3
    client.shutdown()


def test_shutdown_flushes_and_stops_the_thread(ingest: Ingest) -> None:
    client = Totallytics(KEY, flush_interval=3_600)
    record(client)
    worker = client._worker
    assert worker is not None and worker.is_alive()

    assert client.shutdown()
    assert len(ingest.calls) == 1
    assert not worker.is_alive()

    record(client)
    assert client._worker is not None and client._worker.is_alive()
    assert client.shutdown()
    assert len(ingest.calls) == 2
