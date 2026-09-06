import json
from unittest.mock import patch

import pytest

from pico import ModelAction
from pico.providers.clients import OpenAICompatibleModelClient

TOOLS = [
    {"name": "read_file"},
    {"name": "search"},
    {"name": "submit_final"},
]


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def client():
    return OpenAICompatibleModelClient(
        "gpt-test",
        "https://example.test/v1",
        "secret",
        None,
        3,
    )


def multi_call_response():
    return Response(
        {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [], "encrypted_content": "synthetic"},
                {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": "call_a",
                    "arguments": json.dumps({"path": "a.py"}),
                },
                {
                    "type": "function_call",
                    "name": "search",
                    "call_id": "call_b",
                    "arguments": json.dumps({"pattern": "needle", "path": "."}),
                },
            ]
        }
    )


def final_response():
    return Response(
        {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_final",
                    "call_id": "call_final",
                    "arguments": json.dumps({"answer": "done"}),
                }
            ]
        }
    )


def test_provider_parses_ordered_multi_call_response():
    captured = {}

    def urlopen(request, timeout):
        captured.update(json.loads(request.data))
        return multi_call_response()

    with patch("pico.providers.clients._open_response", urlopen):
        action = client().complete_action(
            "inspect",
            64,
            instructions="rules",
            action_tools=TOOLS,
        )

    assert action.kind == "tool"
    assert [(call.call_id, call.name) for call in action.tool_calls] == [
        ("call_a", "read_file"),
        ("call_b", "search"),
    ]
    assert captured["parallel_tool_calls"] is True


def test_provider_returns_all_group_results_in_one_continuation():
    instance = client()
    requests = []

    def urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return multi_call_response() if len(requests) == 1 else final_response()

    with patch("pico.providers.clients._open_response", urlopen):
        action = instance.complete_action(
            "inspect",
            64,
            instructions="rules",
            action_tools=TOOLS,
        )
        assert len(action.tool_calls) == 2
        instance.record_action_results(("result-a", "result-b"))
        final = instance.complete_action(
            "replacement ignored",
            64,
            instructions="rules",
            action_tools=TOOLS,
        )

    assert final == ModelAction.final("done")
    assert requests[1]["input"][1:4] == json.loads(multi_call_response().payload)["output"]
    outputs = [
        item
        for item in requests[1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert outputs == [
        {"type": "function_call_output", "call_id": "call_a", "output": "result-a"},
        {"type": "function_call_output", "call_id": "call_b", "output": "result-b"},
    ]


def test_provider_refuses_partial_group_results():
    instance = client()
    with patch("pico.providers.clients._open_response", return_value=multi_call_response()):
        instance.complete_action(
            "inspect",
            64,
            instructions="rules",
            action_tools=TOOLS,
        )

    try:
        instance.record_action_results(("only-one-result",))
    except ValueError as exc:
        assert "one result per call" in str(exc)
    else:
        raise AssertionError("partial group results must be rejected")


def test_provider_returns_correction_for_missing_group_call_name():
    malformed = Response(
        {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": "call_a",
                    "arguments": json.dumps({"path": "a.py"}),
                },
                {
                    "type": "function_call",
                    "name": "",
                    "call_id": "call_b",
                    "arguments": json.dumps({"path": "b.py"}),
                },
            ]
        }
    )
    with patch("pico.providers.clients._open_response", return_value=malformed):
        action = client().complete_action(
            "inspect",
            64,
            instructions="rules",
            action_tools=TOOLS,
        )

    assert action.kind == "invalid"
    assert action.content == "function call is missing a name"


def test_provider_rejects_unknown_call_anywhere_in_group():
    malformed = Response(
        {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": "call_a",
                    "arguments": json.dumps({"path": "a.py"}),
                },
                {
                    "type": "function_call",
                    "name": "undeclared_tool",
                    "call_id": "call_b",
                    "arguments": "{}",
                },
            ]
        }
    )
    with patch("pico.providers.clients._open_response", return_value=malformed):
        action = client().complete_action(
            "inspect",
            64,
            instructions="rules",
            action_tools=TOOLS,
        )

    assert action.kind == "invalid"
    assert action.content == "unknown function call: undeclared_tool"


@pytest.mark.parametrize("status", [None, "queued", "in_progress", "cancelled"])
def test_nonterminal_responses_cannot_produce_actions(status):
    instance = client()
    response = {"output": [{"type": "function_call", "name": "read_file",
                            "call_id": "unsafe", "arguments": '{"path":"a.py"}'}]}
    if status is not None:
        response["status"] = status
    with patch("pico.providers.clients._open_response", return_value=Response(response)):
        action = instance.complete_action("inspect", 64, instructions="rules", action_tools=TOOLS)
    assert action.kind == "invalid"
    assert not action.tool_calls
    assert instance._pending_call_ids == ()


def test_sse_without_a_terminal_event_is_rejected():
    data = {"type": "response.in_progress", "response": {"status": "in_progress", "output": [
        {"type": "function_call", "name": "read_file", "call_id": "unsafe", "arguments": "{}"},
    ]}}
    with pytest.raises(RuntimeError, match="SSE"):
        client()._decode_response("data: " + json.dumps(data) + "\n\n", "text/event-stream")


def test_sse_terminal_event_cannot_override_a_cancelled_resource():
    data = {"type": "response.completed", "response": {"status": "cancelled", "output": []}}
    with pytest.raises(RuntimeError, match="inconsistent_response_status"):
        client()._decode_response("data: " + json.dumps(data) + "\n\n", "text/event-stream")


@pytest.mark.parametrize("status", ["completed", "incomplete", "in_progress"])
def test_invalid_response_cannot_poison_the_next_request_history(status):
    instance = client()
    requests = []
    # The real child run rejected history with missing text after a no-call
    # response. This fixture reproduces how rejected output poisons replay.
    malformed = Response({"status": status, "output": [{
        "type": "message", "role": "assistant", "content": [{"type": "output_text"}],
    }]})

    def urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return malformed if len(requests) == 1 else final_response()

    with patch("pico.providers.clients._open_response", urlopen):
        invalid = instance.complete_action("inspect", 64, instructions="rules", action_tools=TOOLS)
        assert invalid.kind == "invalid"
        instance.record_action_results((invalid.content,))
        final = instance.complete_action("inspect", 64, instructions="rules", action_tools=TOOLS)

    assert final == ModelAction.final("done")
    assert requests[1]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "inspect"}]},
        {"role": "user", "content": [{"type": "input_text", "text": invalid.content}]},
    ]
