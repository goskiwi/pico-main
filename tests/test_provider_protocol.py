import io
import json
import urllib.error
from http.client import IncompleteRead
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


def tool_response(call_id="call_read"):
    return Response(json.dumps({
        "output": [{
            "type": "function_call",
            "name": "read_file",
            "call_id": call_id,
            "arguments": json.dumps({"path": "README.md"}),
        }]
    }))


def test_action_session_replays_native_output_and_exact_tool_result():
    instance = client()
    requests = []

    def urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return tool_response() if len(requests) == 1 else final_response()

    with patch("urllib.request.urlopen", urlopen):
        action = instance.complete_action("initial prompt", 32, action_tools=TOOLS)
        instance.record_action_result(action, "exact bounded tool result")
        final = instance.complete_action("replacement prompt", 32, action_tools=TOOLS)

    assert final == ModelAction.final("done")
    assert requests[0]["input"][0]["content"][0]["text"] == "initial prompt"
    assert requests[0]["store"] is False
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert requests[1]["input"][0]["content"][0]["text"] == "initial prompt"
    assert requests[1]["input"][1]["type"] == "function_call"
    assert requests[1]["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_read",
        "output": "exact bounded tool result",
    }
    assert "replacement prompt" not in json.dumps(requests[1]["input"])


def test_responses_payload_uses_total_output_token_budget():
    captured = {}

    def urlopen(request, timeout):
        captured.update(json.loads(request.data))
        return final_response()

    with patch("urllib.request.urlopen", urlopen):
        client().complete_action("prompt", 1024, action_tools=TOOLS)

    assert captured["max_output_tokens"] == 1024


def test_openai_adapter_estimates_serialized_tool_schema_tokens():
    serialized = json.dumps(
        TOOLS,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    estimated = client().estimate_action_tool_tokens(TOOLS, lambda text: len(text))

    assert estimated == len(serialized)


def test_action_session_requires_output_or_explicit_reset():
    instance = client()
    with patch("urllib.request.urlopen", return_value=tool_response()):
        instance.complete_action("initial", 32, action_tools=TOOLS)
    with pytest.raises(RuntimeError, match="no recorded output"):
        instance.complete_action("ignored", 32, action_tools=TOOLS)

    instance.reset_action_session()
    with patch("urllib.request.urlopen", return_value=final_response()):
        assert instance.complete_action("fresh", 32, action_tools=TOOLS).kind == "final"


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


def test_non_transient_http_status_is_not_retried_or_echoed():
    error = urllib.error.HTTPError(
        "https://example.test/v1/responses",
        400,
        "bad",
        {},
        io.BytesIO(b"provider echoed secret"),
    )
    with (
        patch("urllib.request.urlopen", side_effect=error) as request,
        pytest.raises(RuntimeError, match=r"HTTP 400") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)
    assert request.call_count == 1
    assert "provider echoed secret" not in str(raised.value)
    assert "secret" not in str(raised.value)


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


def test_retries_share_one_request_deadline():
    instance = client()
    clock = {"value": 0.0}
    timeouts = []

    def monotonic():
        return clock["value"]

    def urlopen(_request, timeout):
        timeouts.append(timeout)
        clock["value"] += timeout
        raise TimeoutError("socket stalled")

    with (
        patch("pico.providers.clients.time.monotonic", monotonic),
        patch("pico.providers.clients.time.sleep") as sleep,
        patch("urllib.request.urlopen", urlopen),
        pytest.raises(RuntimeError, match="Could not reach"),
    ):
        instance._request_with_retry({"model": "test"}, request_timeout=1.0)

    assert timeouts == [1.0]
    assert clock["value"] == 1.0
    sleep.assert_not_called()


def test_incomplete_chunked_response_is_retried():
    with (
        patch(
            "urllib.request.urlopen",
            side_effect=[IncompleteRead(b"partial response"), final_response()],
        ) as request,
        patch("time.sleep") as sleep,
    ):
        action = client().complete_action("prompt", 32, action_tools=TOOLS)

    assert action == ModelAction.final("done")
    assert request.call_count == 2
    sleep.assert_called_once_with(0.5)


@pytest.mark.parametrize("body", ["not-json", "[]", "null"])
def test_invalid_json_response_shape_has_normalized_provider_error(body):
    with (
        patch("urllib.request.urlopen", return_value=Response(body)),
        pytest.raises(RuntimeError, match="OpenAI-compatible error"),
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)


def test_malformed_response_output_has_normalized_provider_error():
    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response(json.dumps({"output": [42]})),
        ),
        pytest.raises(RuntimeError, match="malformed response output"),
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)


def test_sse_provider_error_is_not_treated_as_model_output():
    event = {
        "type": "response.failed",
        "response": {"error": {"message": "provider secret detail"}},
    }
    body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n"

    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response(body, "text/event-stream"),
        ),
        pytest.raises(RuntimeError, match="backend returned an error") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert "provider secret detail" not in str(raised.value)


def test_sse_text_without_response_object_is_rejected():
    body = (
        'data: {"type":"response.output_text.delta","delta":"not a tool"}\n\n'
        "data: [DONE]\n"
    )
    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response(body, "text/event-stream"),
        ),
        pytest.raises(RuntimeError, match="did not contain a response object"),
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)


def test_incomplete_max_token_response_reports_output_truncation():
    action = _action_from_response(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        },
        TOOLS,
    )

    assert action.kind == "invalid"
    assert "one concise function call" in action.content


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ([], "exactly one function call"),
        ([
            {"type": "function_call", "name": "read_file", "call_id": "a", "arguments": {}},
            {"type": "function_call", "name": "read_file", "call_id": "b", "arguments": {}},
        ], "exactly one function call"),
        ([{"type": "function_call", "name": "delete_world", "call_id": "a", "arguments": {}}],
         "unknown function call"),
        ([{"type": "function_call", "name": "read_file", "call_id": "a", "arguments": "{"}],
         "malformed JSON arguments"),
        ([{"type": "function_call", "name": "read_file", "call_id": "a", "arguments": []}],
         "arguments must be an object"),
        ([{"type": "function_call", "name": "read_file", "arguments": {}}],
         "missing a call id"),
    ],
)
def test_invalid_function_call_shapes_are_invalid_actions(output, message):
    action = _action_from_response({"output": output}, TOOLS)
    assert action.kind == "invalid"
    assert message in action.content


def test_usage_and_cache_metadata_are_optional_and_normalized():
    usage = {
        "input_tokens": 11,
        "output_tokens": 4,
        "total_tokens": 15,
        "input_tokens_details": {"cached_tokens": 7},
        "output_tokens_details": {"reasoning_tokens": 3},
    }
    instance = client()
    with patch("urllib.request.urlopen", return_value=final_response(usage=usage)):
        assert instance.complete_action("prompt", 32, action_tools=TOOLS).kind == "final"
    assert instance.last_completion_metadata == {
        "input_tokens": 11,
        "output_tokens": 4,
        "total_tokens": 15,
        "cached_tokens": 7,
    }

    instance = client()
    with patch("urllib.request.urlopen", return_value=final_response()):
        assert instance.complete_action("prompt", 32, action_tools=TOOLS).kind == "final"
    assert instance.last_completion_metadata["input_tokens"] is None
    assert instance.last_completion_metadata["cached_tokens"] == 0


def test_official_prompt_cache_uses_key_without_model_specific_retention_parameter():
    instance = OpenAICompatibleModelClient(
        "gpt-5.6", "https://api.openai.com/v1", "secret", None, 3
    )
    captured = {}

    def urlopen(request, timeout):
        captured.update(json.loads(request.data))
        return final_response()

    with patch("urllib.request.urlopen", urlopen):
        instance.complete_action(
            "prompt",
            32,
            action_tools=TOOLS,
            prompt_cache_key="stable-prefix",
        )

    assert captured["prompt_cache_key"] == "stable-prefix"
    assert "prompt_cache_retention" not in captured
    assert "prompt_cache_options" not in captured


@pytest.mark.parametrize(
    ("base_url", "supported"),
    [
        ("https://api.openai.com/v1", True),
        ("https://www.right.codes/codex/v1", True),
        ("https://openai.com.evil.example/v1", False),
    ],
)
def test_prompt_cache_support_uses_hostname_boundaries(base_url, supported):
    instance = OpenAICompatibleModelClient(
        "gpt-test",
        base_url,
        "secret",
        None,
        3,
    )

    assert instance.supports_prompt_cache is supported
