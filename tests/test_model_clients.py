import json

import httpx
from langchain_core.messages import AIMessage

import pico.cli as cli
from pico.models import OpenAICompatibleModelClient
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


def _response(*output, response_id="resp_1", status="completed", incomplete_details=None):
    response = {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": "gpt-5.6-luna",
        "output": list(output),
    }
    if incomplete_details is not None:
        response["incomplete_details"] = incomplete_details
    return response


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
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "name": name,
        "arguments": json.dumps(args, separators=(",", ":")),
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
            "gpt-5.6-luna",
            "https://rightapi.ai/codex/v1",
            "sk-test",
            0.2,
            30,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
        requests,
    )


def test_openai_client_posts_expected_responses_payload():
    client, requests = _mocked_client(_text_response("ok"))

    assert client.complete("hello", 42) == "ok"
    assert requests[0] == {
        "include": ["reasoning.encrypted_content"],
        "input": [{"content": "hello", "role": "user", "type": "message"}],
        "max_output_tokens": 42,
        "model": "gpt-5.6-luna",
        "store": False,
        "stream": False,
    }


def test_openai_client_sends_reasoning_effort_when_configured():
    queue = [_text_response("ok")]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    client = OpenAICompatibleModelClient(
        "gpt-5.6-luna",
        "https://rightapi.ai/codex/v1",
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


def test_openai_client_rejects_tool_calls_from_max_output_truncation():
    client, _ = _mocked_client(
        _response(
            _call("read_file", {"files": [{"path": "README.md"}]}, "call_1"),
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
        )
    )

    action = client.complete_action("inspect", 100, action_tools=[READ_FILE_TOOL])

    assert action.kind == "retry"
    assert action.call_id == "call_1"
    assert "max_output_tokens" in action.error
    assert client.last_completion_metadata["response_output_truncated"] is True


def test_truncated_tool_call_is_retried_without_local_tool_execution(tmp_path):
    (tmp_path / "README.md").write_text("Pico\n", encoding="utf-8")
    client, requests = _mocked_client(
        _response(
            _call("read_file", {"files": [{"path": "README.md"}]}, "call_1"),
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
        ),
        _response(_call("submit_final", {"answer": "Retried safely."}, "call_2")),
    )
    agent = build_agent(tmp_path, [])
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect README.md and finish.") == "Retried safely."
    assert agent.tool_audit_log == []
    assert requests[1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": (
            "Responses output reached max_output_tokens; none of its function calls were executed. "
            "Return exactly one complete function call."
        ),
    }


def test_openai_client_replays_reasoning_and_function_output():
    reasoning = {
        "id": "reasoning_1",
        "type": "reasoning",
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }
    client, requests = _mocked_client(
        _response(reasoning, _call("read_file", {"files": [{"path": "README.md"}]}, "call_1")),
        _response(_call("submit_final", {"answer": "Done."}, "call_2"), response_id="resp_2"),
    )

    first = client.complete_action("inspect", 100, action_tools=[READ_FILE_TOOL, SUBMIT_FINAL_TOOL])
    client.record_action_result(first, "README contents")
    second = client.complete_action("ignored", 100, action_tools=[READ_FILE_TOOL, SUBMIT_FINAL_TOOL])

    assert second.kind == "final"
    replayed = requests[1]["input"]
    assert any(item.get("encrypted_content") == "encrypted-reasoning" for item in replayed)
    assert replayed[-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "README contents",
    }


def test_openai_client_rejects_malformed_or_multiple_function_calls():
    malformed = AIMessage(
        content=[],
        invalid_tool_calls=[{"name": "read_file", "args": "not-json", "id": "call_1"}],
    )
    multiple = AIMessage(
        content=[],
        tool_calls=[
            {"name": "read_file", "args": {"files": [{"path": "README.md"}]}, "id": "call_1"},
            {"name": "submit_final", "args": {"answer": "Done."}, "id": "call_2"},
        ],
    )

    malformed_action = OpenAICompatibleModelClient._action_from_message(malformed, [READ_FILE_TOOL])
    multiple_action = OpenAICompatibleModelClient._action_from_message(multiple, [READ_FILE_TOOL, SUBMIT_FINAL_TOOL])

    assert malformed_action.kind == "retry"
    assert "malformed JSON" in malformed_action.error
    assert multiple_action.kind == "retry"
    assert "known non-final tools" in multiple_action.error


def test_cli_selects_the_gpt_responses_adapter(monkeypatch):
    captured = {}

    class FakeOpenAIClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", FakeOpenAIClient)
    args = cli.build_arg_parser().parse_args([])

    client = cli._build_model_client(
        args,
        env={
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_API_BASE": "https://rightapi.ai/codex/v1",
            "OPENAI_MODEL": "gpt-5.6-luna",
            "OPENAI_REASONING_EFFORT": "low",
        },
    )

    assert isinstance(client, FakeOpenAIClient)
    assert captured == {
        "model": "gpt-5.6-luna",
        "base_url": "https://rightapi.ai/codex/v1",
        "api_key": "openai-key",
        "temperature": 0.2,
        "timeout": 300,
        "reasoning_effort": "low",
    }


def test_openai_client_runs_through_picos_bounded_tool_loop(tmp_path):
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    client, requests = _mocked_client(
        _response(_call("read_file", {"files": [{"path": "hello.txt"}]}, "call_1")),
        _response(_call("submit_final", {"answer": "Done."}, "call_2"), response_id="resp_2"),
    )
    agent = build_agent(tmp_path, [])
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Read hello.txt and finish.") == "Done."
    assert requests[1]["input"][-1]["call_id"] == "call_1"
