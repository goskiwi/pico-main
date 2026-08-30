import io
import json
import traceback
import urllib.error
from http.client import IncompleteRead
from unittest.mock import patch

import pytest

from pico import ModelAction
from pico.providers import ProviderContextOverflow
from pico.providers.clients import OpenAICompatibleModelClient, _action_from_response

TOOLS = [
    {"name": "read_file"},
    {"name": "submit_final"},
]


@pytest.fixture(autouse=True)
def default_runtime_instructions(monkeypatch):
    original = OpenAICompatibleModelClient.complete_action

    def complete_action(instance, *args, **kwargs):
        kwargs.setdefault("instructions", "stable runtime rules")
        return original(instance, *args, **kwargs)

    monkeypatch.setattr(OpenAICompatibleModelClient, "complete_action", complete_action)


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


def assert_exception_graph_redacted(error, *secrets):
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for secret in secrets:
            assert secret not in str(current)
            assert secret not in rendered
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


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
        "provider secret reason",
        {},
        io.BytesIO(b"provider echoed secret"),
    )
    with (
        patch("urllib.request.urlopen", side_effect=error) as request,
        pytest.raises(RuntimeError, match=r"HTTP 400") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)
    assert request.call_count == 1
    assert_exception_graph_redacted(
        raised.value,
        "provider echoed secret",
        "provider secret reason",
        "secret",
    )


@pytest.mark.parametrize(
    "identifier",
    [
        {"type": "invalid_request_error"},
        {"code": 400},
        {"code": "bad_request"},
    ],
)
def test_http_context_overflow_is_typed_without_echoing_provider_body(identifier):
    error = urllib.error.HTTPError(
        "https://example.test/v1/responses",
        400,
        "provider secret reason",
        {"Content-Type": "application/json"},
        io.BytesIO(
            json.dumps(
                {
                    "error": {
                        **identifier,
                        "message": (
                            "The maximum context length was exceeded; "
                            "provider secret context detail"
                        ),
                    }
                }
            ).encode()
        ),
    )

    with (
        patch("urllib.request.urlopen", side_effect=error) as request,
        pytest.raises(ProviderContextOverflow) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert request.call_count == 1
    assert_exception_graph_redacted(
        raised.value,
        "provider secret context detail",
        "provider secret reason",
        "secret",
    )


def test_http_rate_limit_with_token_wording_retries_and_is_not_overflow():
    def rate_limit_error():
        return urllib.error.HTTPError(
            "https://example.test/v1/responses",
            429,
            "rate limited",
            {"Content-Type": "application/json"},
            io.BytesIO(
                json.dumps(
                    {
                        "error": {
                            "code": "rate_limit_exceeded",
                            "message": "Too many tokens per minute; provider secret",
                        }
                    }
                ).encode()
            ),
        )

    with (
        patch(
            "urllib.request.urlopen",
            side_effect=[rate_limit_error() for _ in range(3)],
        ) as request,
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="HTTP 429") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert request.call_count == 3
    assert not isinstance(raised.value, ProviderContextOverflow)
    assert "provider secret" not in str(raised.value)


@pytest.mark.parametrize("status", [429, 500])
def test_transient_http_context_wording_without_context_code_still_retries(status):
    def transient_error():
        return urllib.error.HTTPError(
            "https://example.test/v1/responses",
            status,
            "provider secret reason",
            {"Content-Type": "application/json"},
            io.BytesIO(
                json.dumps(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": (
                                "The maximum context length was exceeded; "
                                "provider secret"
                            ),
                        }
                    }
                ).encode()
            ),
        )

    with (
        patch(
            "urllib.request.urlopen",
            side_effect=[transient_error() for _ in range(3)],
        ) as request,
        patch("time.sleep"),
        pytest.raises(RuntimeError, match=f"HTTP {status}") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert request.call_count == 3
    assert not isinstance(raised.value, ProviderContextOverflow)
    assert_exception_graph_redacted(raised.value, "provider secret", "secret")


def test_transient_http_structured_context_code_is_typed_without_retry():
    error = urllib.error.HTTPError(
        "https://example.test/v1/responses",
        500,
        "provider secret reason",
        {"Content-Type": "application/json"},
        io.BytesIO(
            json.dumps(
                {
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "provider secret",
                    }
                }
            ).encode()
        ),
    )

    with (
        patch("urllib.request.urlopen", side_effect=error) as request,
        pytest.raises(ProviderContextOverflow) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert request.call_count == 1
    assert_exception_graph_redacted(raised.value, "provider secret", "secret")


def test_incomplete_http_error_body_is_discarded_and_redacted():
    class IncompleteBody:
        @staticmethod
        def read():
            raise IncompleteRead(b"provider secret partial body", 100)

        @staticmethod
        def close():
            return None

    error = urllib.error.HTTPError(
        "https://example.test/v1/responses",
        400,
        "provider secret reason",
        {"Content-Type": "application/json"},
        IncompleteBody(),
    )

    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(RuntimeError, match="HTTP 400") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert not isinstance(raised.value, ProviderContextOverflow)
    assert_exception_graph_redacted(
        raised.value,
        "provider secret partial body",
        "provider secret reason",
        "secret",
    )


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


def test_network_error_traceback_does_not_leak_provider_reason_or_api_key():
    with (
        patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("provider secret network reason"),
        ) as request,
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="Could not reach") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert request.call_count == 3
    assert_exception_graph_redacted(
        raised.value,
        "provider secret network reason",
        "secret",
    )


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


def test_non_json_provider_body_is_absent_from_exception_graph():
    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response("provider secret non-json body"),
        ),
        pytest.raises(RuntimeError, match="non-JSON") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert_exception_graph_redacted(
        raised.value,
        "provider secret non-json body",
        "secret",
    )


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


def test_json_context_overflow_error_is_typed_and_redacted():
    response = Response(
        json.dumps(
            {
                "error": {
                    "code": "context_length_exceeded",
                    "message": (
                        "This model's maximum context length is 8192 tokens. "
                        "provider secret detail"
                    ),
                }
            }
        )
    )

    with (
        patch("urllib.request.urlopen", return_value=response),
        pytest.raises(ProviderContextOverflow) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert "provider secret detail" not in str(raised.value)
    assert "secret" not in str(raised.value)


def test_top_level_json_error_uses_its_own_context_payload():
    response = Response(
        json.dumps(
            {
                "type": "error",
                "code": "context_length_exceeded",
                "message": "provider secret detail",
            }
        )
    )

    with (
        patch("urllib.request.urlopen", return_value=response),
        pytest.raises(ProviderContextOverflow) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert_exception_graph_redacted(raised.value, "provider secret detail", "secret")


@pytest.mark.parametrize("error", [None, {}, "", False])
def test_failed_response_status_is_error_even_with_falsey_error(error):
    response = Response(json.dumps({"status": "failed", "error": error, "output": []}))

    with (
        patch("urllib.request.urlopen", return_value=response),
        pytest.raises(RuntimeError, match="backend returned an error") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert not isinstance(raised.value, ProviderContextOverflow)


def test_sse_context_overflow_error_is_typed_and_redacted():
    event = {
        "type": "response.failed",
        "response": {
            "error": {
                "type": "invalid_request_error",
                "message": (
                    "The maximum context length was exceeded; "
                    "provider secret detail"
                ),
            }
        },
    }
    body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n"

    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response(body, "text/event-stream"),
        ),
        pytest.raises(ProviderContextOverflow) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert "provider secret detail" not in str(raised.value)
    assert "secret" not in str(raised.value)


def test_failed_sse_envelope_cannot_smuggle_a_valid_function_call():
    event = {
        "type": "response.failed",
        "response": {
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_final",
                    "call_id": "call_false_success",
                    "arguments": {"answer": "must not be accepted"},
                }
            ]
        },
    }
    body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n"

    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response(body, "text/event-stream"),
        ),
        pytest.raises(RuntimeError, match="backend returned an error"),
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)


def test_top_level_sse_context_message_is_typed():
    event = {
        "type": "error",
        "message": "The maximum context length was exceeded; provider secret",
    }
    body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n"

    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response(body, "text/event-stream"),
        ),
        pytest.raises(ProviderContextOverflow) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert "provider secret" not in str(raised.value)


def test_sse_parser_merges_data_lines_and_normalizes_content_type():
    body = (
        'data: {"type":"response.failed",\n'
        'data: "response":{"error":{"code":"context_length_exceeded",\n'
        'data: "message":"provider secret detail"}}}\n\n'
        "data: [DONE]\n\n"
    )

    with (
        patch(
            "urllib.request.urlopen",
            return_value=Response(body, "Text/Event-Stream; Charset=UTF-8"),
        ),
        pytest.raises(ProviderContextOverflow) as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert_exception_graph_redacted(raised.value, "provider secret detail", "secret")


def test_ordinary_provider_error_is_not_context_overflow():
    response = Response(
        json.dumps(
            {
                "error": {
                    "code": "invalid_api_key",
                    "message": (
                        "The maximum context length was exceeded; "
                        "provider secret authentication detail"
                    ),
                }
            }
        )
    )

    with (
        patch("urllib.request.urlopen", return_value=response),
        pytest.raises(RuntimeError, match="backend returned an error") as raised,
    ):
        client().complete_action("prompt", 32, action_tools=TOOLS)

    assert not isinstance(raised.value, ProviderContextOverflow)
    assert "provider secret authentication detail" not in str(raised.value)


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


def test_multiple_function_calls_with_one_call_id_leave_no_pending_or_orphan():
    instance = client()
    response = Response(
        json.dumps(
            {
                "output": [
                    {"type": "reasoning", "encrypted_content": "opaque"},
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "call_read",
                        "arguments": {"path": "README.md"},
                    },
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": {"path": "README.md"},
                    },
                ]
            }
        )
    )

    with patch("urllib.request.urlopen", return_value=response):
        action = instance.complete_action("prompt", 32, action_tools=TOOLS)

    assert action.kind == "invalid"
    assert instance._pending_call_id is None
    assert all(
        item.get("type") != "function_call"
        for item in instance._action_input
        if isinstance(item, dict)
    )

    instance.record_action_result(action, "return exactly one function call")
    assert instance._action_input[-1]["role"] == "user"
    assert instance._action_input[-1]["content"][0]["text"] == (
        "return exactly one function call"
    )


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
        "uncached_input_tokens": 4,
    }

    instance = client()
    with patch("urllib.request.urlopen", return_value=final_response()):
        assert instance.complete_action("prompt", 32, action_tools=TOOLS).kind == "final"
    assert instance.last_completion_metadata["input_tokens"] is None
    assert instance.last_completion_metadata["cached_tokens"] is None
    assert instance.last_completion_metadata["uncached_input_tokens"] is None


def test_openai_compatible_client_creates_fresh_isolated_session():
    instance = client()
    instance._action_input.append({"type": "prior"})

    isolated = instance.new_isolated_client()

    assert isolated is not instance
    assert isolated.model == instance.model
    assert isolated.base_url == instance.base_url
    assert isolated.api_key == instance.api_key
    assert isolated._action_input == []


def test_remaining_run_time_only_caps_configured_provider_timeout():
    instance = client()
    observed = {}

    def urlopen(_request, timeout):
        observed["timeout"] = timeout
        return final_response()

    with patch("urllib.request.urlopen", urlopen):
        instance.complete_action(
            "prompt",
            32,
            action_tools=TOOLS,
            request_timeout=100,
        )

    assert observed["timeout"] <= instance.timeout


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
