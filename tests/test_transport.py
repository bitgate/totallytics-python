"""Talks to a real local HTTP server, so urllib's request and error paths are covered too."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import totallytics._transport as transport
from totallytics import Totallytics, __version__

from .conftest import KEY, record

REAL_POST = transport.post


class Server:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 202
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                reply = b'{"accepted":{"metrics":1,"errors":0},"rejected":0}'
                self.send_response(server.status)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/ingest"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(transport, "post", REAL_POST)
    instance = Server()
    thread = threading.Thread(target=instance.httpd.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.httpd.shutdown()
    instance.httpd.server_close()


def test_posts_json_with_bearer_auth_and_user_agent(server: Server) -> None:
    client = Totallytics(KEY, endpoint=server.url, integration="test")
    record(client)
    assert client.flush(5)

    (request,) = server.requests
    assert request["path"] == "/api/ingest"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["User-Agent"] == f"totallytics-python/{__version__}"
    assert json.loads(request["body"])["sdk"] == f"totallytics-python/{__version__} test"


def test_reads_error_statuses(server: Server) -> None:
    server.status = 401
    assert REAL_POST(server.url, KEY, b"{}", 5)[0] == 401
    server.status = 503
    assert REAL_POST(server.url, KEY, b"{}", 5)[0] == 503


def test_retries_when_the_endpoint_is_unreachable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    with pytest.raises(OSError):
        REAL_POST(f"http://127.0.0.1:{closed_port}/api/ingest", KEY, b"{}", 2)
