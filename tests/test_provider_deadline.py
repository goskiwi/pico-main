import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pico.providers.clients import OpenAICompatibleModelClient


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
            self.wfile.write(b"{}")

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
        body, _headers = client._request_with_retry({}, request_timeout=1)
        assert body == "{}"
    finally:
        server.shutdown()
        server.server_close()
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
            client._request_with_retry({}, request_timeout=0.15)
        assert time.monotonic() - started < 0.7
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
