"""Exercise Pico's real SDK/HTTP transport against a controlled loopback server."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pico.execution import ExecutionContext
from pico.providers.clients import OpenAICompatibleModelClient


def exercise(stage, *, deadline=False):
    ready = threading.Event()
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.close_connection = True
            if len(requests) == 1:
                if stage == "sse_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b': connection established\n\n')
                    self.wfile.flush()
                ready.set()
                release.wait(10)
                return
            response = {
                "id": "resp_local", "object": "response", "created_at": 0,
                "status": "completed", "model": "local-test",
                "output": [{"type": "function_call", "id": "fc_local",
                            "call_id": "call_local", "name": "submit_final",
                            "arguments": '{"answer":"transport recovered"}', "status": "completed"}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            payload = ('data: ' + json.dumps({"type": "response.completed", "sequence_number": 0,
                                            "response": response}) + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client = OpenAICompatibleModelClient("local-test", f"http://127.0.0.1:{server.server_port}/v1",
                                        "local-dummy-key", None, 8)
    context = ExecutionContext.root(max_seconds=0.7 if deadline else 8)
    tools = [{"type": "function", "name": "submit_final", "parameters": {
        "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}]
    observed = {}
    def request():
        try:
            observed["action"] = client.complete_action("first", 100, instructions="test", action_tools=tools,
                                                       execution_context=context).kind
        except Exception as exc:  # noqa: BLE001 - return worker failures to the test thread
            observed["exception"] = type(exc).__name__
        finally:
            observed["finished_at"] = time.monotonic()

    worker = threading.Thread(target=request, daemon=True)
    try:
        worker.start()
        if not ready.wait(3):
            raise RuntimeError("server never received first request")
        # Allow the SDK to enter its blocking receive before requesting cancellation.
        time.sleep(0.15)
        cancelled_at = time.monotonic()
        if not deadline:
            context.request_stop()
        worker.join(1)
        prompt_cancel = not worker.is_alive()
        release.set()
        worker.join(10)
        if worker.is_alive():
            raise RuntimeError("request did not settle after server release")
        client.reset_action_session()  # Same Pico client; new task after cancellation.
        action = client.complete_action("second", 100, instructions="test", action_tools=tools,
                                        execution_context=ExecutionContext.root(max_seconds=3))
        return {"stage": stage, "cancel_returned_within_1s": prompt_cancel,
                "cancel_exception": observed.get("exception"),
                "cancel_elapsed_seconds": round(observed["finished_at"] - cancelled_at, 3),
                "server_released_after_wait": not prompt_cancel,
                "next_request_passed": action.kind == "final" and action.content == "transport recovered",
                "http_requests": len(requests)}
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join(1)


class HttpCancellationTests(unittest.TestCase):
    def test_silent_sse_respects_deadline_then_request_again(self):
        result = exercise("sse_body", deadline=True)
        self.assertTrue(result["cancel_returned_within_1s"], result)
        self.assertEqual(result["cancel_exception"], "ExecutionDeadlineExceeded", result)
        self.assertTrue(result["next_request_passed"], result)

    def test_cancel_while_waiting_for_headers_then_request_again(self):
        self._assert_cancel("response_headers")

    def test_cancel_silent_sse_then_request_again(self):
        self._assert_cancel("sse_body")

    def _assert_cancel(self, stage):
        result = exercise(stage)
        self.assertTrue(result["cancel_returned_within_1s"], result)
        self.assertEqual(result["cancel_exception"], "ExecutionCancelled", result)
        self.assertTrue(result["next_request_passed"], result)
        self.assertEqual(result["http_requests"], 2, result)
