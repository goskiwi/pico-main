import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from pico import ModelAction
from pico.providers.clients import OpenAICompatibleModelClient, _action_from_response

TOOLS = [
    {"name": "read_file"},
    {"name": "submit_final"},
]


class Response:
    def __init__(self, payload, content_type="application/json"):
        self.payload = payload
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload.encode()


def client():
    return OpenAICompatibleModelClient(
        "gpt-test", "https://example.test", "secret", None, 3
    )


def final_response(answer="done", *, usage=None):
    data = {
        "output": [{
            "type": "function_call",
            "name": "submit_final",
            "call_id": "call_final",
            "arguments": json.dumps({"answer": answer}),
        }]
    }
    if usage is not None:
        data["usage"] = usage
    return Response(json.dumps(data))


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_http_status_is_retried(status):
    error = urllib.error.HTTPError(
        "https://example.test/v1/responses", status, "transient", {}, io.BytesIO(b"busy")
    )
    with (
        patch("urllib.request.urlopen", side_effect=[error, final_response()]) as request,
        patch("time.sleep") as sleep,
    ):
        assert client().complete_action("prompt", 32, action_tools=TOOLS) == ModelAction.final("done")
    assert request.call_count == 2
    sleep.assert_called_once_with(0.5)


def test_non_transient_http_status_is_not_retried_and_body_is_reported():
    error = urllib.error.HTTPError(
        "https://example.test/v1/responses", 400, "bad", {}, io.BytesIO(b"bad schema")
    )
    with (
        patch("urllib.request.urlopen", side_effect=error) as request,
        pytest.raises(RuntimeError, match=r"HTTP 400: bad schema"),
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)
    assert request.call_count == 1


def test_timeout_is_retried_then_normalized_without_leaking_api_key():
    with (
        patch("urllib.request.urlopen", side_effect=TimeoutError("socket stalled")) as request,
        patch("time.sleep"),
        pytest.raises(RuntimeError) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)
    assert request.call_count == 3
    assert "Could not reach" in str(raised.value)
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("body", ["not-json", "[]", "null"])
def test_invalid_json_response_shape_has_normalized_provider_error(body):
    with (
        patch("urllib.request.urlopen", return_value=Response(body)),
        pytest.raises(RuntimeError, match="OpenAI-compatible error"),
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)


def test_sse_text_when_tool_is_required_becomes_retry_action():
    body = (
        'data: {"type":"response.output_text.delta","delta":"not a tool"}\n\n'
        "data: [DONE]\n"
    )
    with patch(
        "urllib.request.urlopen", return_value=Response(body, "text/event-stream")
    ):
        action = client().complete_action("prompt", 32, action_tools=TOOLS)
    assert action.kind == "retry"
    assert action.error == "invalid_function_call_count"


@pytest.mark.parametrize(
    ("output", "error"),
    [
        ([], "invalid_function_call_count"),
        ([
            {"type": "function_call", "name": "read_file", "call_id": "a", "arguments": {}},
            {"type": "function_call", "name": "read_file", "call_id": "b", "arguments": {}},
        ], "invalid_function_call_count"),
        ([{"type": "function_call", "name": "delete_world", "call_id": "a", "arguments": {}}],
         "unknown_function_call"),
        ([{"type": "function_call", "name": "read_file", "call_id": "a", "arguments": "{"}],
         "malformed_function_arguments"),
        ([{"type": "function_call", "name": "read_file", "call_id": "a", "arguments": []}],
         "invalid_function_arguments"),
        ([{"type": "function_call", "name": "read_file", "arguments": {}}],
         "missing_function_call_id"),
    ],
)
def test_invalid_function_call_shapes_are_retry_actions(output, error):
    action = _action_from_response({"output": output}, TOOLS)
    assert action.kind == "retry"
    assert action.error == error


def test_usage_and_cache_metadata_are_optional_and_normalized():
    usage = {
        "prompt_tokens": 11,
        "completion_tokens": 4,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 7},
    }
    instance = client()
    with patch("urllib.request.urlopen", return_value=final_response(usage=usage)):
        assert instance.complete_action("prompt", 32, action_tools=TOOLS).kind == "final"
    assert instance.last_completion_metadata == {
        "prompt_cache_supported": False,
        "prompt_cache_key": None,
        "prompt_cache_retention": None,
        "input_tokens": 11,
        "output_tokens": 4,
        "total_tokens": 15,
        "cached_tokens": 7,
        "cache_hit": True,
    }

    instance = client()
    with patch("urllib.request.urlopen", return_value=final_response()):
        assert instance.complete_action("prompt", 32, action_tools=TOOLS).kind == "final"
    assert instance.last_completion_metadata["input_tokens"] is None
    assert instance.last_completion_metadata["cached_tokens"] == 0
