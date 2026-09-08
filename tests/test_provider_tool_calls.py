import json
from unittest.mock import patch

import pytest

from pico import ModelAction
from pico.execution import ExecutionContext
from pico.providers.clients import OpenAICompatibleModelClient, ProviderHTTPError

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


def execution_context():
    return ExecutionContext.root(max_seconds=30)


def complete_action(instance, input_text="inspect"):
    return instance.complete_action(
        input_text,
        64,
        instructions="rules",
        action_tools=TOOLS,
        execution_context=execution_context(),
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

    def urlopen(request, timeout, execution_context):
        execution_context.check_active()
        captured.update(json.loads(request.data))
        return multi_call_response()

    with patch("pico.providers.clients._open_response", urlopen):
        action = complete_action(client())

    assert action.kind == "tool"
    assert [(call.call_id, call.name) for call in action.tool_calls] == [
        ("call_a", "read_file"),
        ("call_b", "search"),
    ]
    assert captured["parallel_tool_calls"] is True


def test_provider_returns_all_group_results_in_one_continuation():
    instance = client()
    requests = []

    def urlopen(request, timeout, execution_context):
        execution_context.check_active()
        requests.append(json.loads(request.data))
        return multi_call_response() if len(requests) == 1 else final_response()

    with patch("pico.providers.clients._open_response", urlopen):
        action = complete_action(instance)
        assert len(action.tool_calls) == 2
        instance.record_action_results(("result-a", "result-b"))
        final = complete_action(instance, "replacement ignored")

    assert final == ModelAction.final("done")
    assert requests[1]["input"][1:4] == [
        {"type": "reasoning", "summary": [], "encrypted_content": "synthetic"},
        {
            "type": "function_call",
            "call_id": "call_a",
            "name": "read_file",
            "arguments": '{"path":"a.py"}',
        },
        {
            "type": "function_call",
            "call_id": "call_b",
            "name": "search",
            "arguments": '{"path":".","pattern":"needle"}',
        },
    ]
    outputs = [
        item
        for item in requests[1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert outputs == [
        {"type": "function_call_output", "call_id": "call_a", "output": "result-a"},
        {"type": "function_call_output", "call_id": "call_b", "output": "result-b"},
    ]


def test_provider_normalizes_assistant_preamble_before_tool_replay():
    instance = client()
    requests = []
    response = Response({
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_preamble",
                "status": "completed",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "text": "I will inspect the file.",
                    "logprobs": [],
                }],
                "provider_metadata": {"ignored": True},
            },
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "call_read",
                "arguments": '{"path":"README.md"}',
            },
        ],
    })

    def urlopen(request, timeout, execution_context):
        execution_context.check_active()
        requests.append(json.loads(request.data))
        return response if len(requests) == 1 else final_response()

    with patch("pico.providers.clients._open_response", urlopen):
        action = complete_action(instance)
        instance.record_action_results(("observed",))
        assert complete_action(instance) == ModelAction.final("done")

    assert action.kind == "tool"
    assert requests[1]["input"][1:3] == [
        {
            "type": "message",
            "id": "msg_preamble",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": "I will inspect the file.",
                "annotations": [],
            }],
        },
        {
            "type": "function_call",
            "call_id": "call_read",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
        },
    ]


def test_provider_refuses_partial_group_results():
    instance = client()
    with patch("pico.providers.clients._open_response", return_value=multi_call_response()):
        complete_action(instance)

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
        action = complete_action(client())

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
        action = complete_action(client())

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
        action = complete_action(instance)
    assert action.kind == "invalid"
    assert not action.tool_calls
    assert instance._pending_call_ids == ()


def test_sse_without_a_terminal_event_is_rejected():
    data = {"type": "response.in_progress", "response": {"status": "in_progress", "output": [
        {"type": "function_call", "name": "read_file", "call_id": "unsafe", "arguments": "{}"},
    ]}}
    with pytest.raises(ProviderHTTPError, match="SSE") as caught:
        client()._decode_response("data: " + json.dumps(data) + "\n\n", "text/event-stream")
    assert caught.value.transient


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
    malformed = Response({"status": status, "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text"}],
        },
        {
            "type": "function_call",
            "name": "read_file",
            "call_id": "call_looks_valid",
            "arguments": '{"path":"a.py"}',
        },
    ]})

    def urlopen(request, timeout, execution_context):
        execution_context.check_active()
        requests.append(json.loads(request.data))
        return malformed if len(requests) == 1 else final_response()

    with patch("pico.providers.clients._open_response", urlopen):
        invalid = complete_action(instance)
        assert invalid.kind == "invalid"
        instance.record_action_results((invalid.content,))
        final = complete_action(instance)

    assert final == ModelAction.final("done")
    assert requests[1]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "inspect"}]},
        {"role": "user", "content": [{"type": "input_text", "text": invalid.content}]},
    ]


def test_rejected_output_tokens_do_not_enter_context_projection():
    instance = client()
    response = Response({
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 60},
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text"}],
        }],
    })

    with patch("pico.providers.clients._open_response", return_value=response):
        action = complete_action(instance)

    assert action.kind == "invalid"
    assert instance.projected_context_tokens(
        (action.content,),
        instructions="rules",
        action_tools=TOOLS,
        token_counter=lambda _text: 1,
    ) == 101


def test_context_estimate_owns_usage_and_expires_after_continuation_or_reset():
    instance = client()
    response = Response({
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 60},
        "output": [{"type": "function_call", "name": "read_file",
                    "call_id": "read", "arguments": '{"path":"README.md"}'}],
    })
    options = {"instructions": "rules", "action_tools": TOOLS, "token_counter": lambda _: 1}
    with patch("pico.providers.clients._open_response", return_value=response):
        complete_action(instance)
        # Diagnostic metadata is not the session's estimation state.
        instance.last_completion_metadata["input_tokens"] = 9999
        assert instance.projected_context_tokens(("observed",), **options) == 161
        instance.record_action_results(("observed",))
        assert instance.projected_context_tokens(("continue",), **options) == 1
        complete_action(instance)
        assert instance.projected_context_tokens(("observed",), **options) == 161
        instance.reset_action_session()
        assert instance.projected_context_tokens(("continue",), **options) == 1


@pytest.mark.parametrize(
    "calls",
    [
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "call_read",
                "arguments": '{"path":"README.md"}',
            },
            {
                "type": "function_call",
                "name": "submit_final",
                "call_id": "call_final",
                "arguments": '{"answer":"too early"}',
            },
        ],
        [
            {
                "type": "function_call",
                "name": "submit_final",
                "call_id": "call_final_a",
                "arguments": '{"answer":"first"}',
            },
            {
                "type": "function_call",
                "name": "submit_final",
                "call_id": "call_final_b",
                "arguments": '{"answer":"second"}',
            },
        ],
    ],
)
def test_submit_final_groups_are_rejected_before_replay(calls):
    instance = client()
    response = Response({"status": "completed", "output": calls})

    with patch("pico.providers.clients._open_response", return_value=response):
        action = complete_action(instance)

    assert action.kind == "invalid"
    assert action.content == "submit_final must be the only call in its model response"
    assert instance._pending_call_ids == ()
    assert instance._action_input == [
        {"role": "user", "content": [{"type": "input_text", "text": "inspect"}]}
    ]
