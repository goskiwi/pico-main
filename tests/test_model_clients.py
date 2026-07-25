import json

import httpx
from langchain_core.messages import AIMessage

from pico.models import OpenAICompatibleModelClient


READ_FILE_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": "Read a file.",
    "parameters": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["files"],
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


def _response(*output, response_id="resp_1"):
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-test",
        "output": list(output),
    }


def _text_response(text):
    return _response(
        {
            "id": "msg_1",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
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


def _mocked_client(*responses):
    queue = list(responses)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    return (
        OpenAICompatibleModelClient(
            "right.codes/codex-mini",
            "https://right.codes/v1",
            "sk-test",
            0.2,
            30,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        requests,
    )


def test_openai_compatible_client_posts_expected_responses_payload():
    client, requests = _mocked_client(_text_response("ok"))

    assert client.complete("hello", 42) == "ok"
    assert requests[0] == {
        "include": ["reasoning.encrypted_content"],
        "input": [{"content": "hello", "role": "user", "type": "message"}],
        "max_output_tokens": 42,
        "model": "right.codes/codex-mini",
        "store": False,
        "stream": False,
        "temperature": 0.2,
    }


def test_openai_compatible_client_sends_reasoning_effort_when_configured():
    queue = [_text_response("ok")]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    client = OpenAICompatibleModelClient(
        "right.codes/codex-mini",
        "https://right.codes/v1",
        "sk-test",
        0.2,
        30,
        reasoning_effort="low",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.complete("hello", 42) == "ok"
    assert requests[0]["reasoning"] == {"effort": "low"}


def test_openai_client_requires_one_strict_function_call():
    client, requests = _mocked_client(
        _response(_call("read_file", {"files": [{"path": "README.md"}]}, "call_1"))
    )

    action = client.complete_action("inspect", 100, action_tools=[READ_FILE_TOOL])

    assert (action.kind, action.name, action.args) == (
        "tool",
        "read_file",
        {"files": [{"path": "README.md"}]},
    )
    assert requests[0]["tool_choice"] == "required"
    assert requests[0]["parallel_tool_calls"] is False
    assert requests[0]["tools"] == [READ_FILE_TOOL]


def test_openai_client_replays_reasoning_and_function_output():
    reasoning = {
        "id": "reasoning_1",
        "type": "reasoning",
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }
    client, requests = _mocked_client(
        _response(reasoning, _call("read_file", {"files": [{"path": "README.md"}]}, "call_1")),
        _response(
            _call("submit_final", {"answer": "Done."}, "call_2"),
            response_id="resp_2",
        ),
    )
    tools = [READ_FILE_TOOL, SUBMIT_FINAL_TOOL]

    first = client.complete_action("inspect", 100, action_tools=tools)
    client.record_action_result(first, "README contents")
    second = client.complete_action("ignored rebuilt prompt", 100, action_tools=tools)

    assert second.kind == "final"
    replayed = requests[1]["input"]
    assert any(item.get("encrypted_content") == "encrypted-reasoning" for item in replayed)
    assert replayed[-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "README contents",
    }


def test_openai_client_executes_first_call_and_defers_extras():
    client, requests = _mocked_client(
        _response(
            _call("read_file", {"files": [{"path": "README.md"}]}, "call_1"),
            _call("read_file", {"files": [{"path": "pyproject.toml"}]}, "call_2"),
        ),
        _response(
            _call("read_file", {"files": [{"path": "README.md"}]}, "call_3"),
            response_id="resp_2",
        ),
    )

    first = client.complete_action("inspect", 100, action_tools=[READ_FILE_TOOL])
    client.record_action_result(first, "README contents")
    client.complete_action("ignored", 100, action_tools=[READ_FILE_TOOL])

    assert requests[1]["input"][-1]["call_id"] == "call_2"
    assert requests[1]["input"][-1]["output"].startswith("deferred_by_runtime")


def test_openai_client_rejects_multiple_calls_when_one_is_final():
    message = AIMessage(
        content=[],
        tool_calls=[
            {"name": "read_file", "args": {"files": [{"path": "README.md"}]}, "id": "call_1"},
            {"name": "submit_final", "args": {"answer": "Done."}, "id": "call_2"},
        ],
    )

    action = OpenAICompatibleModelClient._action_from_message(
        message,
        [READ_FILE_TOOL, SUBMIT_FINAL_TOOL],
    )

    assert action.kind == "retry"
    assert "known non-final tools" in action.error


def test_openai_client_audits_malformed_function_arguments():
    message = AIMessage(
        content=[],
        invalid_tool_calls=[
            {
                "name": "read_file",
                "args": "not-json",
                "id": "call_1",
                "error": "invalid JSON",
            }
        ],
    )

    action = OpenAICompatibleModelClient._action_from_message(
        message,
        [READ_FILE_TOOL],
    )

    assert action.kind == "retry"
    assert "malformed JSON" in action.error
    assert action.raw_preview
