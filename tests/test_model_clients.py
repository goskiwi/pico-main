import json
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage

from pico.models import OpenAICompatibleModelClient


READ_FILE_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": "Read a file.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "strict": True,
}
SUBMIT_FINAL_TOOL = {
    "type": "function",
    "name": "submit_final",
    "description": "Finish the task.",
    "parameters": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    },
    "strict": True,
}


def _response(*output, response_id="resp_1", usage=None):
    payload = {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-test",
        "output": list(output),
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _text_response(text, **kwargs):
    return _response(
        {
            "id": "msg_1",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
        **kwargs,
    )


def _call(name, args, call_id):
    arguments = args if isinstance(args, str) else json.dumps(args, separators=(",", ":"))
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }


def _mocked_client(*responses, api_key="sk-test", base_url="https://right.codes/v1"):
    queue = list(responses)
    requests = []

    def handler(request):
        requests.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
                "timeout": request.extensions.get("timeout", {}),
            }
        )
        response = queue.pop(0)
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleModelClient(
        "right.codes/codex-mini",
        base_url,
        api_key,
        0.2,
        30,
        http_client=http_client,
    )
    return client, requests


def test_openai_delegate_fork_has_independent_action_state_and_transport():
    client, requests = _mocked_client(_text_response("child response"))
    client._action_pending_call_ids = ["parent-call"]

    child = client.fork_for_delegate()

    assert child is not client
    assert child.model == client.model
    assert child.base_url == client.base_url
    assert child._action_pending_call_ids == []
    assert client._action_pending_call_ids == ["parent-call"]
    assert child.complete("child prompt", 20) == "child response"
    assert len(requests) == 1


def test_openai_compatible_client_closes_only_owned_transport():
    client = OpenAICompatibleModelClient("gpt-test", "https://example.test", "sk-test", 0, 30)
    transport = client._http_client

    client.close()

    assert transport.is_closed


def test_openai_compatible_client_posts_expected_responses_payload():
    client, requests = _mocked_client(_text_response("<final>ok</final>"))

    assert client.complete("hello", 42) == "<final>ok</final>"

    request = requests[0]
    assert request["url"] == "https://right.codes/v1/responses"
    assert request["timeout"]["read"] == 30
    assert request["headers"]["authorization"] == "Bearer sk-test"
    assert request["body"] == {
        "include": ["reasoning.encrypted_content"],
        "input": [{"content": "hello", "role": "user", "type": "message"}],
        "max_output_tokens": 42,
        "model": "right.codes/codex-mini",
        "store": False,
        "stream": False,
        "temperature": 0.2,
    }


def test_openai_compatible_client_omits_authorization_without_api_key():
    client, requests = _mocked_client(
        _text_response("ok"), api_key="", base_url="http://localhost:8000"
    )

    assert client.complete("hello", 10) == "ok"
    assert "authorization" not in requests[0]["headers"]


def test_openai_compatible_client_retries_server_errors():
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        if len(attempts) == 1:
            return httpx.Response(
                502,
                headers={"retry-after-ms": "0"},
                json={"error": {"message": "temporary outage", "type": "server_error"}},
            )
        return httpx.Response(200, json=_text_response("ok"))

    client = OpenAICompatibleModelClient(
        "gpt-test",
        "https://right.codes/v1",
        "sk-test",
        0,
        30,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.complete("hello", 42) == "ok"
    assert len(attempts) == 2


def test_openai_compatible_client_uses_required_strict_function_call():
    client, requests = _mocked_client(
        _response(_call("read_file", {"path": "README.md"}, "call_1"))
    )

    action = client.complete_action("inspect", 100, action_tools=[READ_FILE_TOOL])

    assert (action.kind, action.name, action.args) == (
        "tool",
        "read_file",
        {"path": "README.md"},
    )
    body = requests[0]["body"]
    assert body["tool_choice"] == "required"
    assert body["parallel_tool_calls"] is False
    assert body["tools"] == [READ_FILE_TOOL]
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["store"] is False and body["stream"] is False
    assert client.last_completion_metadata["structured_action"] is True


def test_openai_compatible_client_replays_reasoning_and_function_output():
    reasoning = {
        "id": "reasoning_1",
        "type": "reasoning",
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }
    client, requests = _mocked_client(
        _response(reasoning, _call("read_file", {"path": "README.md"}, "call_1")),
        _response(
            _call("submit_final", {"answer": "Done."}, "call_2"), response_id="resp_2"
        ),
    )
    tools = [READ_FILE_TOOL, SUBMIT_FINAL_TOOL]

    first = client.complete_action("inspect", 100, action_tools=tools)
    client.record_action_result(first, "README contents")
    second = client.complete_action("ignored rebuilt prompt", 100, action_tools=tools)

    assert second.kind == "final"
    assert "previous_response_id" not in requests[1]["body"]
    replayed = requests[1]["body"]["input"]
    assert any(item.get("encrypted_content") == "encrypted-reasoning" for item in replayed)
    assert replayed[-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "README contents",
    }


def test_openai_compatible_client_executes_first_call_and_defers_extras():
    client, requests = _mocked_client(
        _response(
            _call("read_file", {"path": "README.md"}, "call_1"),
            _call("read_file", {"path": "pyproject.toml"}, "call_2"),
        ),
        _response(_call("read_file", {"path": "README.md"}, "call_3"), response_id="resp_2"),
    )

    first = client.complete_action("inspect", 100, action_tools=[READ_FILE_TOOL])
    assert client.last_completion_metadata["deferred_function_calls"] == 1
    client.record_action_result(first, "README contents")
    retried = client.complete_action("ignored", 100, action_tools=[READ_FILE_TOOL])

    assert first.call_id == "call_1" and retried.kind == "tool"
    assert requests[1]["body"]["input"][-2:] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "README contents"},
        {
            "type": "function_call_output",
            "call_id": "call_2",
            "output": (
                "deferred_by_runtime: only the first function call is executed; "
                "call this function again if it is still needed"
            ),
        },
    ]


def test_openai_compatible_client_rejects_multiple_calls_with_final():
    message = AIMessage(
        content=[],
        tool_calls=[
            {"name": "read_file", "args": {"path": "README.md"}, "id": "call_1"},
            {"name": "submit_final", "args": {"answer": "Done."}, "id": "call_2"},
        ],
    )

    action = OpenAICompatibleModelClient._action_from_message(
        message, [READ_FILE_TOOL, SUBMIT_FINAL_TOOL]
    )

    assert action.kind == "retry"
    assert "known non-final tools" in action.error


def test_openai_compatible_client_maps_submit_final_function():
    message = AIMessage(
        content=[],
        tool_calls=[{"name": "submit_final", "args": {"answer": "Done."}, "id": "call_1"}],
    )

    action = OpenAICompatibleModelClient._action_from_message(message, [SUBMIT_FINAL_TOOL])

    assert action.kind == "final"
    assert action.answer == "Done."


def test_openai_compatible_client_audits_malformed_function_arguments():
    message = AIMessage(
        content=[],
        invalid_tool_calls=[
            {"name": "read_file", "args": "not-json", "id": "call_1", "error": "invalid JSON"}
        ],
    )

    action = OpenAICompatibleModelClient._action_from_message(message, [READ_FILE_TOOL])

    assert action.kind == "retry"
    assert "malformed JSON" in action.error
    assert action.raw_preview


def test_openai_compatible_client_records_prompt_cache_usage():
    usage = {
        "input_tokens": 2048,
        "input_tokens_details": {"cached_tokens": 1536},
        "output_tokens": 32,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 2080,
    }
    response = _text_response("ok", usage=usage)
    response["service_tier"] = "priority"
    client, requests = _mocked_client(response)

    result = client.complete(
        "hello", 42, prompt_cache_key="prefix-hash-123", prompt_cache_retention="in_memory"
    )

    assert result == "ok"
    assert requests[0]["body"]["prompt_cache_key"] == "prefix-hash-123"
    assert requests[0]["body"]["prompt_cache_retention"] == "in_memory"
    assert client.last_completion_metadata["cached_tokens"] == 1536
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_completion_metadata["input_tokens"] == 2048


def test_openai_compatible_client_accepts_completed_sse_for_nonstreaming_request():
    completed = {"type": "response.completed", "response": _text_response("stream ok")}
    body = f"event: response.completed\ndata: {json.dumps(completed)}\n\ndata: [DONE]\n\n"
    client, requests = _mocked_client(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    )

    assert client.complete("hello", 42) == "stream ok"
    assert requests[0]["body"]["stream"] is False


def test_openai_compatible_client_accepts_sse_deltas_for_nonstreaming_request():
    body = (
        "event: response.created\n"
        'data: {"type":"response.created","response":{"status":"in_progress","output":[]}}\n\n'
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"<final>"}\n\n'
        "event: response.output_text.done\n"
        'data: {"type":"response.output_text.done","text":"<final>OK</final>"}\n\n'
    )
    client, requests = _mocked_client(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    )

    assert client.complete("hello", 42) == "<final>OK</final>"
    assert requests[0]["body"]["stream"] is False


def test_openai_compatible_client_disables_ambient_langsmith_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    client, _ = _mocked_client(_text_response("private"))

    with patch("langchain_core.tracers.langchain.LangChainTracer") as tracer:
        assert client.complete("do not trace", 20) == "private"

    tracer.assert_not_called()
