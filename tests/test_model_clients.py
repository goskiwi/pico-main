import json

import httpx
from langchain_core.messages import AIMessage

import pico.cli as cli
from pico.models import DeepSeekChatCompletionsModelClient, OpenAICompatibleModelClient
from tests.helpers import build_agent


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


def _chat_response(message, response_id="chatcmpl_1"):
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "message": message,
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
        },
    }


def _chat_call(name, args, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, separators=(",", ":")),
        },
    }


def _chat_tool(response_tool):
    return {
        "type": "function",
        "function": {
            "name": response_tool["name"],
            "description": response_tool["description"],
            "parameters": response_tool["parameters"],
        },
    }


def _mocked_deepseek_client(*responses):
    queue = list(responses)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    return (
        DeepSeekChatCompletionsModelClient(
            "deepseek-v4-flash",
            "https://api.deepseek.com",
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


def test_deepseek_client_uses_chat_completions_non_thinking_tool_calls():
    client, requests = _mocked_deepseek_client(
        _chat_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_chat_call("read_file", {"files": [{"path": "README.md"}]}, "call_1")],
            }
        )
    )
    tools = [_chat_tool(READ_FILE_TOOL)]

    action = client.complete_action("inspect", 100, action_tools=tools)

    assert (action.kind, action.name, action.args, action.protocol) == (
        "tool",
        "read_file",
        {"files": [{"path": "README.md"}]},
        "deepseek_chat_function",
    )
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["max_tokens"] == 100
    assert requests[0]["tool_choice"] == "required"
    assert requests[0]["tools"] == tools
    assert "strict" not in requests[0]["tools"][0]["function"]


def test_deepseek_client_replays_tool_output_to_the_chat_conversation():
    client, requests = _mocked_deepseek_client(
        _chat_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_chat_call("read_file", {"files": [{"path": "README.md"}]}, "call_1")],
            }
        ),
        _chat_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_chat_call("submit_final", {"answer": "Done."}, "call_2")],
            },
            response_id="chatcmpl_2",
        ),
    )
    tools = [_chat_tool(READ_FILE_TOOL), _chat_tool(SUBMIT_FINAL_TOOL)]

    first = client.complete_action("inspect", 100, action_tools=tools)
    client.record_action_result(first, "README contents")
    second = client.complete_action("ignored", 100, action_tools=tools)

    assert second.kind == "final"
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "content": "README contents",
        "tool_call_id": "call_1",
    }


def test_cli_selects_the_deepseek_chat_completions_adapter(monkeypatch):
    captured = {}

    class FakeDeepSeekClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(cli, "DeepSeekChatCompletionsModelClient", FakeDeepSeekClient)
    args = cli.build_arg_parser().parse_args(["--provider", "deepseek"])

    client = cli._build_model_client(
        args,
        env={
            "DEEPSEEK_API_KEY": "deepseek-key",
            "DEEPSEEK_MODEL": "deepseek-v4-pro",
        },
    )

    assert isinstance(client, FakeDeepSeekClient)
    assert captured == {
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
        "api_key": "deepseek-key",
        "temperature": 0.2,
        "timeout": 300,
    }


def test_deepseek_client_runs_through_picos_bounded_tool_loop(tmp_path):
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    client, requests = _mocked_deepseek_client(
        _chat_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_chat_call("read_file", {"files": [{"path": "hello.txt"}]}, "call_1")],
            }
        ),
        _chat_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_chat_call("submit_final", {"answer": "Done."}, "call_2")],
            },
            response_id="chatcmpl_2",
        ),
    )
    agent = build_agent(tmp_path, [])
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Read hello.txt and finish.") == "Done."

    assert {tool["function"]["name"] for tool in requests[0]["tools"]} >= {
        "read_file",
        "submit_final",
    }
    replayed_tool_result = requests[1]["messages"][-1]
    assert replayed_tool_result["role"] == "tool"
    assert replayed_tool_result["tool_call_id"] == "call_1"
    assert "hello" in replayed_tool_result["content"]
