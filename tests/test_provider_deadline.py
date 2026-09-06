import json
import threading
import time
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pico.providers.clients import (
    OpenAICompatibleModelClient,
    ProviderHTTPError,
    ProviderTransportError,
)


def test_streaming_only_provider_returns_completed_tool_call_over_http():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((payload, self.headers.get("Accept")))
            if payload.get("stream") is not True:
                self.send_error(400, "streaming requests only")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [
                {"type": "response.created", "response": {"status": "in_progress", "output": []}},
                {"type": "response.function_call_arguments.delta", "delta": '{"path":'},
                {"type": "response.completed", "response": {
                    "status": "completed", "output": [{
                        "type": "function_call", "name": "read_file", "call_id": "call_read",
                        "arguments": '{"path":"a.py"}',
                    }], "usage": {"input_tokens": 10, "output_tokens": 5},
                }},
            ]
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OpenAICompatibleModelClient(
            "test", f"http://127.0.0.1:{server.server_port}", "", None, 2,
        )
        action = client.complete_action(
            "inspect", 64, instructions="rules", action_tools=[{"name": "read_file"}],
        )
        assert action.kind == "tool"
        assert [(call.call_id, call.args) for call in action.tool_calls] == [("call_read", {"path": "a.py"})]
        assert len(requests) == 1
        assert requests[0][1] == "text/event-stream"
        assert client.last_completion_metadata["input_tokens"] == 10
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_deadline_transport_preserves_http_redirects():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"completed","output":[]}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OpenAICompatibleModelClient(
            "test",
            f"http://127.0.0.1:{server.server_port}",
            "",
            None,
            2,
        )
        response = client._request_response({}, request_timeout=1)
        assert response == {"status": "completed", "output": []}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cross_origin_redirect_does_not_receive_credentials_or_body():
    received = []

    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/other")
            self.end_headers()

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    servers = (origin, destination)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    try:
        client = OpenAICompatibleModelClient("test", f"http://127.0.0.1:{origin.server_port}",
                                             "synthetic-token", None, 2)
        with pytest.raises(ProviderTransportError, match="cross-origin"):
            client._request_response({"input": "private request body"}, 2)
        assert received == []
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_provider_total_deadline_interrupts_continuous_slow_response(phase):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                if phase == "headers":
                    data = (
                        b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 100
                    )
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    data = b"x" * 100
                for byte in data:
                    time.sleep(0.02)
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = OpenAICompatibleModelClient(
        "test",
        f"http://127.0.0.1:{server.server_port}",
        "",
        None,
        2,
    )
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            client._request_response({}, request_timeout=0.15)
        assert time.monotonic() - started < 0.7
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_provider_transport_error_preserves_cause_and_attempt_count(monkeypatch):
    @contextmanager
    def failed_open(_request, timeout):
        del timeout
        raise urllib.error.URLError("DNS lookup failed")
        yield

    monkeypatch.setattr("pico.providers.clients._open_response", failed_open)
    monkeypatch.setattr("pico.providers.clients.time.sleep", lambda _seconds: None)
    client = OpenAICompatibleModelClient(
        "test", "https://example.test/v1", "", None, 10
    )

    with pytest.raises(ProviderTransportError) as caught:
        client._request_response({}, request_timeout=10)

    assert "3 attempts" in str(caught.value)
    assert "URLError: <urlopen error DNS lookup failed>" in str(caught.value)
    assert isinstance(caught.value.__cause__, urllib.error.URLError)


def test_provider_response_error_preserves_code_and_message():
    body = '{"error":{"code":"unsupported_parameter","message":"temperature is unsupported"}}'

    with pytest.raises(ProviderHTTPError) as caught:
        OpenAICompatibleModelClient._decode_response(body, "application/json")

    assert "code=unsupported_parameter" in str(caught.value)
    assert "message=temperature is unsupported" in str(caught.value)


def test_response_level_transient_error_retries_inside_one_deadline(monkeypatch):
    bodies = iter(
        (
            b'{"error":{"code":"server_error","message":"overloaded"}}',
            b'{"error":{"type":"overloaded","message":"try later"}}',
            b'{"status":"completed","output":[]}',
        )
    )
    attempts = []
    delays = []

    class Response:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

        def read(self):
            attempts.append(1)
            return next(bodies)

    @contextmanager
    def open_response(_request, timeout):
        assert timeout > 0
        yield Response()

    monkeypatch.setattr("pico.providers.clients._open_response", open_response)
    monkeypatch.setattr(
        "pico.providers.clients.time.sleep",
        lambda seconds: delays.append(seconds),
    )
    client = OpenAICompatibleModelClient(
        "test", "https://example.test/v1", "", None, 10
    )

    response = client._request_response({}, request_timeout=10)

    assert response == {"status": "completed", "output": []}
    assert len(attempts) == 3
    assert delays == [0.5, 1.0]


def test_response_level_permanent_error_is_not_retried(monkeypatch):
    attempts = []

    class Response:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

        def read(self):
            attempts.append(1)
            return b'{"error":{"code":"unsupported_parameter","message":"bad"}}'

    @contextmanager
    def open_response(_request, timeout):
        assert timeout > 0
        yield Response()

    monkeypatch.setattr("pico.providers.clients._open_response", open_response)
    client = OpenAICompatibleModelClient(
        "test", "https://example.test/v1", "", None, 10
    )

    with pytest.raises(ProviderHTTPError, match="unsupported_parameter"):
        client._request_response({}, request_timeout=10)

    assert len(attempts) == 1
