import io
import json
import urllib.error
from unittest.mock import patch

from pico import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient


def test_openai_delegate_fork_has_independent_action_state():
    client = OpenAICompatibleModelClient(
        "gpt-test", "https://api.openai.com/v1", "sk-test", 0, 30
    )
    client._action_pending_call_id = "parent-call"

    child = client.fork_for_delegate()

    assert child is not client
    assert child.model == client.model
    assert child.base_url == client.base_url
    assert child._action_pending_call_id == ""
    assert client._action_pending_call_id == "parent-call"


def test_ollama_client_posts_expected_payload():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"response": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OllamaModelClient(
        model="qwen3.5:4b",
        host="http://127.0.0.1:11434",
        temperature=0.2,
        top_p=0.9,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == 30
    assert captured["body"]["model"] == "qwen3.5:4b"
    assert captured["body"]["prompt"] == "hello"
    assert captured["body"]["stream"] is False


def test_openai_compatible_client_posts_expected_responses_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://right.codes/v1/responses"
    assert captured["timeout"] == 30
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "right.codes/codex-mini",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_output_tokens": 42,
        "stream": False,
        "temperature": 0.2,
    }


def test_openai_compatible_client_retries_server_errors():
    attempts = []

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        attempts.append((request.full_url, timeout))
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                502,
                "bad gateway",
                hdrs={},
                fp=io.BytesIO(b"temporary outage"),
            )
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen), patch("pico.models.time.sleep"):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert len(attempts) == 2


def test_openai_compatible_client_uses_one_required_strict_function_call():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "read_file",
                            "arguments": '{"path":"README.md","start":1,"end":20}',
                            "call_id": "call_1",
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        del timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    tools = [
        {
            "type": "function",
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["path", "start", "end"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    client = OpenAICompatibleModelClient("gpt-test", "https://right.codes/v1", "sk-test", 0, 30)

    with patch("urllib.request.urlopen", fake_urlopen):
        action = client.complete_action("inspect", 100, action_tools=tools)

    assert action.kind == "tool"
    assert action.name == "read_file"
    assert action.args == {"path": "README.md", "start": 1, "end": 20}
    assert action.protocol == "responses_function"
    assert captured["body"]["tool_choice"] == "required"
    assert captured["body"]["parallel_tool_calls"] is False
    assert captured["body"]["tools"] == tools
    assert client.last_completion_metadata["structured_action"] is True


def test_openai_compatible_client_continues_with_client_managed_function_output():
    captured = []

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    responses = [
        {
            "id": "resp_1",
            "output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                    "call_id": "call_1",
                }
            ],
        },
        {
            "id": "resp_2",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_final",
                    "arguments": '{"answer":"Done."}',
                    "call_id": "call_2",
                }
            ],
        },
    ]

    def fake_urlopen(request, timeout):
        del timeout
        captured.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    tools = [{"type": "function", "name": "read_file"}]
    client = OpenAICompatibleModelClient("gpt-test", "https://right.codes/v1", "sk-test", 0, 30)
    with patch("urllib.request.urlopen", fake_urlopen):
        first = client.complete_action("inspect", 100, action_tools=tools)
        client.record_action_result(first, "README contents")
        second = client.complete_action("ignored rebuilt prompt", 100, action_tools=tools)

    assert second.kind == "final"
    assert "previous_response_id" not in captured[1]
    assert captured[1]["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "inspect"}],
        },
        {
            "type": "function_call",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
            "call_id": "call_1",
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "README contents",
        }
    ]


def test_openai_compatible_client_maps_submit_final_function():
    data = {
        "output": [
            {
                "type": "function_call",
                "name": "submit_final",
                "arguments": '{"answer":"Implemented and verified."}',
            }
        ]
    }

    action = OpenAICompatibleModelClient._action_from_response(data, "", [])

    assert action.kind == "final"
    assert action.answer == "Implemented and verified."


def test_openai_compatible_client_audits_malformed_function_arguments():
    data = {
        "output": [
            {
                "type": "function_call",
                "name": "read_file",
                "arguments": "not-json",
            }
        ]
    }

    action = OpenAICompatibleModelClient._action_from_response(
        data,
        "",
        [{"name": "read_file"}],
    )

    assert action.kind == "retry"
    assert "malformed JSON" in action.error
    assert action.raw_preview


def test_openai_compatible_client_sends_prompt_cache_fields_and_records_usage():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "output_text": "<final>ok</final>",
                    "usage": {
                        "input_tokens": 2048,
                        "input_tokens_details": {"cached_tokens": 1536},
                        "output_tokens": 32,
                        "total_tokens": 2080,
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete(
            "hello",
            42,
            prompt_cache_key="prefix-hash-123",
            prompt_cache_retention="in_memory",
        )

    assert result == "<final>ok</final>"
    assert captured["body"]["prompt_cache_key"] == "prefix-hash-123"
    assert captured["body"]["prompt_cache_retention"] == "in_memory"
    assert client.last_completion_metadata["prompt_cache_supported"] is True
    assert client.last_completion_metadata["cached_tokens"] == 1536
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_completion_metadata["input_tokens"] == 2048


def test_openai_compatible_client_extracts_text_from_event_stream():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                'data: {"type":"response.created","response":{"id":"resp_1","output":[]}}\n'
                'data: {"type":"response.completed","response":{"output":[{"content":[{"text":"<final>stream ok</final>"}]}]}}\n'
                "data: [DONE]\n"
            ).encode("utf-8")

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>stream ok</final>"


def test_openai_compatible_client_extracts_text_from_event_stream_deltas():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"<final>"}\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"OK"}\n'
                'event: response.output_text.done\n'
                'data: {"type":"response.output_text.done","text":"<final>OK</final>"}\n'
                "data: [DONE]\n"
            ).encode("utf-8")

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>OK</final>"


def test_anthropic_compatible_client_posts_expected_messages_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "<final>ok</final>",
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://www.right.codes/claude-aws/v1/messages"
    assert captured["timeout"] == 30
    assert captured["headers"]["X-api-key"] == "sk-test"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "claude-sonnet-4-5-20250929",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_tokens": 42,
        "stream": False,
        "temperature": 0.2,
    }


def test_anthropic_compatible_client_extracts_first_text_block():
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "<final>ok</final>"},
                    ]
                }
            ).encode("utf-8")

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
