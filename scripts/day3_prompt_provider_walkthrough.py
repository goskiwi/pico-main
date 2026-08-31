"""Day 3: inspect Prompt channels and the Responses provider protocol.

This walkthrough never contacts a real network. It uses the real
OpenAICompatibleModelClient and replaces only ``urllib.request.urlopen`` with
small deterministic HTTP responses.
"""

import hashlib
import io
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pico import (
    ModelAction,
    OpenAICompatibleModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.providers import ProviderContextOverflow
from pico.run_lifecycle import RunLifecycle

READ_ONLY_TASK = {
    "task_kind": "read_only",
    "requires_workspace_change": False,
    "requires_verification": False,
}
NO_CHANGE_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": False,
    "requires_verification": False,
}


class Response:
    """The tiny subset of an HTTP response used by the provider adapter."""

    def __init__(self, payload):
        self.payload = json.dumps(payload)
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload.encode("utf-8")


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def new_client():
    """Build the verified-capability adapter; urlopen is always patched."""
    return OpenAICompatibleModelClient(
        model="gpt-demo",
        base_url="https://www.rightapi.ai/v1",
        api_key="not-sent-to-a-network",
        temperature=None,
        timeout=5,
    )


def function_call(name, call_id, arguments):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
    }


def response_with_call(name, call_id, arguments, *, usage=None):
    payload = {"output": [function_call(name, call_id, arguments)]}
    if usage is not None:
        payload["usage"] = usage
    return Response(payload)


def input_item_types(input_items):
    return [item.get("type", item.get("role")) for item in input_items]


def context_overflow_http_error():
    """Return a fresh structured HTTP 400; its body can be read only once."""
    body = json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
                "message": "provider-private context detail",
            }
        }
    ).encode("utf-8")
    return urllib.error.HTTPError(
        "https://example.test/v1/responses",
        400,
        "provider-private reason",
        {"Content-Type": "application/json"},
        io.BytesIO(body),
    )


def build_prompt_fixture(root):
    """Build Prompt and schemas inside a real read-only Run."""
    client = new_client()
    bootstrap = Pico(
        model_client=client,
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "prompt-session"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="",
            max_new_tokens=96,
        ),
    )
    RunLifecycle(bootstrap).initialize("Read README.md", **READ_ONLY_TASK)
    action_tools = tuple(bootstrap.tools.action_schemas)
    allowed_tool_names = tuple(
        tool["name"] for tool in bootstrap.tools.model_action_tools()
    )
    prompt, metadata = bootstrap.prompt.build(
        "Read README.md",
        action_tools=action_tools,
    )
    declared_names = {tool["name"] for tool in action_tools}
    allowed_names = set(allowed_tool_names)
    expected_tool_tokens = client.estimate_action_tool_tokens(
        action_tools,
        bootstrap.prompt.context.tokenizer.count,
    )

    assert metadata["section_order"] == [
        "runtime_policy",
        "untrusted_context",
        "task_request",
    ]
    assert metadata["included_context_sections"] == ["workspace"]
    assert "latest_user_request" not in prompt.input_text
    assert prompt.input_text.count("Read README.md") == 1
    assert '"task_kind": "read_only"' in prompt.input_text
    assert metadata["tool_schema_tokens"] == expected_tool_tokens
    assert {"write_file", "edit_file"}.issubset(declared_names)
    assert {"write_file", "edit_file"}.isdisjoint(allowed_names)
    return prompt, metadata, action_tools, allowed_tool_names


def experiment_channels_and_pending(
    prompt,
    metadata,
    action_tools,
    allowed_tool_names,
):
    """A + B: show the three channels and one native continuation."""
    client = new_client()
    requests = []
    pending_timeline = [{"moment": "请求前", "pending_call_id": None}]

    def urlopen(request, timeout):
        requests.append(
            {
                "timeout": timeout,
                "payload": json.loads(request.data.decode("utf-8")),
            }
        )
        if len(requests) == 1:
            return response_with_call(
                "read_file",
                "call_readme",
                {"path": "README.md", "start_line": 1, "end_line": 20},
                usage={
                    "input_tokens": 240,
                    "output_tokens": 18,
                    "total_tokens": 258,
                    "input_tokens_details": {
                        "cached_tokens": 0,
                    },
                },
            )
        return response_with_call(
            "submit_final",
            "call_final",
            {"answer": "README was read through Responses."},
            usage={
                "input_tokens": 280,
                "output_tokens": 16,
                "total_tokens": 296,
                "input_tokens_details": {
                    "cached_tokens": 220,
                },
            },
        )

    with patch("urllib.request.urlopen", urlopen):
        action = client.complete_action(
            prompt.input_text,
            96,
            instructions=prompt.instructions,
            action_tools=action_tools,
            allowed_tool_names=allowed_tool_names,
            prompt_cache_key=metadata["prompt_cache_key"],
        )
        first_cache_metrics = dict(client.last_completion_metadata)
        pending_timeline.append(
            {
                "moment": "收到 read_file function_call 后",
                "pending_call_id": client._pending_call_id,
            }
        )
        client.record_action_result(
            action,
            '{"status":"success","content":"# Provider demo"}',
        )
        pending_timeline.append(
            {
                "moment": "记录 function_call_output 后",
                "pending_call_id": client._pending_call_id,
            }
        )
        final = client.complete_action(
            "这段替换 Prompt 不应进入已经开始的 continuation",
            96,
            instructions=prompt.instructions,
            action_tools=action_tools,
            allowed_tool_names=allowed_tool_names,
            prompt_cache_key=metadata["prompt_cache_key"],
        )
        continued_cache_metrics = dict(client.last_completion_metadata)

    first_payload = requests[0]["payload"]
    continued_payload = requests[1]["payload"]
    first_input = first_payload["input"]
    continued_input = continued_payload["input"]
    read_schema = next(tool for tool in action_tools if tool["name"] == "read_file")

    assert action.kind == "tool"
    assert action.tool_call.name == "read_file"
    assert final == ModelAction.final("README was read through Responses.")
    assert pending_timeline == [
        {"moment": "请求前", "pending_call_id": None},
        {
            "moment": "收到 read_file function_call 后",
            "pending_call_id": "call_readme",
        },
        {"moment": "记录 function_call_output 后", "pending_call_id": None},
    ]
    assert read_schema["strict"] is True
    assert read_schema["parameters"]["additionalProperties"] is False
    assert first_payload["instructions"] == prompt.instructions
    assert continued_payload["instructions"] == prompt.instructions
    assert first_input[0]["content"][0]["text"] == prompt.input_text
    assert input_item_types(continued_input) == [
        "user",
        "function_call",
        "function_call_output",
    ]
    assert "替换 Prompt" not in json.dumps(continued_input, ensure_ascii=False)
    assert first_payload["parallel_tool_calls"] is False
    assert first_payload["tools"] == continued_payload["tools"]
    assert [tool["name"] for tool in first_payload["tools"]] == [
        tool["name"] for tool in action_tools
    ]
    assert first_payload["prompt_cache_key"] == metadata["prompt_cache_key"]
    assert continued_payload["prompt_cache_key"] == metadata["prompt_cache_key"]
    assert first_payload["tool_choice"] == continued_payload["tool_choice"]
    assert first_payload["tool_choice"] == {
        "type": "allowed_tools",
        "mode": "required",
        "tools": [{"type": "function", "name": name} for name in allowed_tool_names],
    }
    assert first_cache_metrics["cached_tokens"] == 0
    assert continued_cache_metrics["cached_tokens"] == 220
    assert continued_cache_metrics["uncached_input_tokens"] == 60

    print_section(
        "A. instructions / input / tools 是三个独立通道",
        {
            "flow": (
                "稳定规则 → instructions | 最小首轮上下文 → input | "
                "稳定原生 Schema → tools | 动态准入 → allowed_tools"
            ),
            "instructions": {
                "characters": len(prompt.instructions),
                "sha256_prefix": hashlib.sha256(
                    prompt.instructions.encode("utf-8")
                ).hexdigest()[:12],
                "same_across_two_turns": (
                    first_payload["instructions"] == continued_payload["instructions"]
                ),
            },
            "input": {
                "section_order": metadata["section_order"],
                "included_context_sections": metadata["included_context_sections"],
                "first_item_types": input_item_types(first_input),
                "task_request": "Read README.md",
                "task_request_occurrences": prompt.input_text.count("Read README.md"),
                "latest_user_request_present": False,
            },
            "tools": {
                "declared_names": [tool["name"] for tool in action_tools],
                "allowed_names": list(allowed_tool_names),
                "declared_but_disallowed_by_read_only_contract": [
                    "write_file",
                    "edit_file",
                ],
                "wire_schema_stable_across_continuation": (
                    first_payload["tools"] == continued_payload["tools"]
                ),
                "wire_schema_tokens": metadata["tool_schema_tokens"],
                "read_file_strict": read_schema["strict"],
                "read_file_required": read_schema["parameters"]["required"],
                "additional_properties": read_schema["parameters"][
                    "additionalProperties"
                ],
                "tool_choice": first_payload["tool_choice"],
                "parallel_tool_calls": first_payload["parallel_tool_calls"],
            },
            "cache": {
                "prompt_cache_key_stable": (
                    first_payload["prompt_cache_key"]
                    == continued_payload["prompt_cache_key"]
                ),
                "first_turn": first_cache_metrics,
                "continued_turn": continued_cache_metrics,
                "continued_cache_hit_ratio": (
                    continued_cache_metrics["cached_tokens"]
                    / continued_cache_metrics["input_tokens"]
                ),
            },
        },
    )
    print_section(
        "B1. 一个 pending call 如何完成续接",
        {
            "flow": "function_call → pending=call_readme → function_call_output → pending=None",
            "pending_timeline": pending_timeline,
            "second_request_input_types": input_item_types(continued_input),
            "replacement_prompt_was_ignored": True,
        },
    )

    # submit_final is transported as a function call too. A real new Run resets
    # the provider session; this explicit reset keeps this experiment isolated.
    client.reset_action_session()


def experiment_multiple_calls_are_rejected(
    prompt,
    action_tools,
    allowed_tool_names,
):
    """B: prove that two function calls cannot leave one orphan pending call."""
    client = new_client()
    payload = {
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque"},
            function_call(
                "read_file",
                "call_valid",
                {"path": "README.md", "start_line": 1, "end_line": 20},
            ),
            {
                "type": "function_call",
                "name": "read_file",
                # Deliberately missing call_id.
                "arguments": json.dumps(
                    {"path": "README.md", "start_line": 1, "end_line": 20}
                ),
            },
        ]
    }

    with patch("urllib.request.urlopen", return_value=Response(payload)):
        action = client.complete_action(
            prompt.input_text,
            96,
            instructions=prompt.instructions,
            action_tools=action_tools,
            allowed_tool_names=allowed_tool_names,
        )

    retained_function_calls = [
        item
        for item in client._action_input
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    assert action.kind == "invalid"
    assert "exactly one function call" in action.content
    assert client._pending_call_id is None
    assert retained_function_calls == []

    print_section(
        "B2. 多 Function Call 不会留下 orphan",
        {
            "provider_returned_function_calls": 2,
            "parsed_action": action.kind,
            "correction": action.content,
            "pending_call_id": client._pending_call_id,
            "retained_function_calls": len(retained_function_calls),
        },
    )


def experiment_incomplete_is_rejected(
    prompt,
    action_tools,
    allowed_tool_names,
):
    """C: an incomplete response cannot smuggle in a complete-looking final."""
    client = new_client()
    requests = []

    incomplete = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            function_call(
                "submit_final",
                "call_partial_final",
                {"answer": "This looks complete, but must not be accepted."},
            )
        ],
    }

    def urlopen(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        if len(requests) == 1:
            return Response(incomplete)
        return response_with_call(
            "submit_final",
            "call_valid_final",
            {"answer": "Accepted only after a complete provider response."},
        )

    with patch("urllib.request.urlopen", urlopen):
        invalid = client.complete_action(
            prompt.input_text,
            96,
            instructions=prompt.instructions,
            action_tools=action_tools,
            allowed_tool_names=allowed_tool_names,
        )
        pending_after_incomplete = client._pending_call_id
        retained_after_incomplete = [
            item
            for item in client._action_input
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        client.record_action_result(invalid, invalid.content)
        corrected = client.complete_action(
            "replacement prompt is ignored inside the continuation",
            96,
            instructions=prompt.instructions,
            action_tools=action_tools,
            allowed_tool_names=allowed_tool_names,
        )

    correction_item = requests[1]["input"][-1]
    correction_text = correction_item["content"][0]["text"]
    assert invalid.kind == "invalid"
    assert "one concise function call" in invalid.content
    assert pending_after_incomplete is None
    assert retained_after_incomplete == []
    assert correction_item["role"] == "user"
    assert correction_text == invalid.content
    assert corrected == ModelAction.final(
        "Accepted only after a complete provider response."
    )

    print_section(
        "C. incomplete 伪 final 必须拒绝",
        {
            "provider_status": "incomplete",
            "looked_like": "submit_final",
            "parsed_action": invalid.kind,
            "pending_after_incomplete": pending_after_incomplete,
            "retained_function_calls": len(retained_after_incomplete),
            "correction_sent_back_as": correction_item["role"],
            "correction": correction_text,
            "next_complete_response": corrected.kind,
        },
    )
    client.reset_action_session()


def overflow_agent(root, client, session_name):
    return Pico(
        model_client=client,
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / session_name),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="",
            max_new_tokens=96,
        ),
    )


def reset_events(agent):
    events = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    return [event for event in events if event.kind == "provider_session_reset"]


def experiment_context_overflow(root):
    """D: classify at the adapter, then recover exactly once in AgentLoop."""
    success_root = root / "overflow-success"
    success_root.mkdir()
    (success_root / "README.md").write_text("overflow demo\n", encoding="utf-8")
    success_client = new_client()
    success_requests = []

    def overflow_once(request, timeout):
        success_requests.append(json.loads(request.data.decode("utf-8")))
        if len(success_requests) == 1:
            raise context_overflow_http_error()
        return response_with_call(
            "submit_final",
            "call_after_overflow",
            {"answer": "Recovered after one typed context overflow."},
        )

    success_agent = overflow_agent(
        success_root,
        success_client,
        "success-sessions",
    )
    with patch("urllib.request.urlopen", overflow_once):
        outcome = success_agent.ask(
            "Return a short provider recovery confirmation",
            **NO_CHANGE_TASK,
        )

    success_resets = reset_events(success_agent)
    assert outcome.status == "completed"
    assert outcome.answer == "Recovered after one typed context overflow."
    assert len(success_requests) == 2
    assert [event.payload["reason"] for event in success_resets] == [
        "context_overflow_retry"
    ]

    print_section(
        "D1. 一次 typed context overflow：重建后成功",
        {
            "flow": (
                "HTTP 400 structured error → ProviderContextOverflow → "
                "AgentLoop reset Provider session/Prompt → retry → completed"
            ),
            "task_contract": (
                "modify + no required workspace change; isolate the Provider retry"
            ),
            "http_request_count": len(success_requests),
            "provider_session_reset_count": len(success_resets),
            "reset_reason": success_resets[0].payload["reason"],
            "run_status": outcome.status,
            "answer": outcome.answer,
        },
    )

    failure_root = root / "overflow-twice"
    failure_root.mkdir()
    (failure_root / "README.md").write_text("overflow twice\n", encoding="utf-8")
    failure_client = new_client()
    failure_requests = []

    def always_overflow(request, timeout):
        failure_requests.append(json.loads(request.data.decode("utf-8")))
        raise context_overflow_http_error()

    failure_agent = overflow_agent(
        failure_root,
        failure_client,
        "failure-sessions",
    )
    caught = None
    with patch("urllib.request.urlopen", always_overflow):
        try:
            failure_agent.ask(
                "Return a short provider recovery confirmation",
                **NO_CHANGE_TASK,
            )
        except ProviderContextOverflow as exc:
            caught = exc

    failure_resets = reset_events(failure_agent)
    assert isinstance(caught, ProviderContextOverflow)
    assert str(caught) == ("OpenAI-compatible error: provider context window exceeded")
    assert "provider-private" not in str(caught)
    assert len(failure_requests) == 2
    assert [event.payload["reason"] for event in failure_resets] == [
        "context_overflow_retry"
    ]

    print_section(
        "D2. 连续两次 typed overflow：第二次向外抛",
        {
            "flow": "overflow → reset once → overflow again → raise",
            "http_request_count": len(failure_requests),
            "provider_session_reset_count": len(failure_resets),
            "raised_type": type(caught).__name__,
            "redacted_message": str(caught),
            "unfinished_run_preserved_for_recovery": failure_agent.run.resumable,
        },
    )


def main():
    print(
        "Day 3 总流程：\n"
        "Prompt 三通道 → 单 Pending 续接 → 非法响应纠正 → "
        "Context Overflow 恢复"
    )
    with tempfile.TemporaryDirectory(prefix="pico-day3-") as directory:
        root = Path(directory)
        (root / "README.md").write_text(
            "# Provider demo\n\nRead this file through a native function call.\n",
            encoding="utf-8",
        )
        (
            prompt,
            metadata,
            action_tools,
            allowed_tool_names,
        ) = build_prompt_fixture(root)

        experiment_channels_and_pending(
            prompt,
            metadata,
            action_tools,
            allowed_tool_names,
        )
        experiment_multiple_calls_are_rejected(
            prompt,
            action_tools,
            allowed_tool_names,
        )
        experiment_incomplete_is_rejected(
            prompt,
            action_tools,
            allowed_tool_names,
        )
        experiment_context_overflow(root)

        print("\nDay 3 完成：所有断言通过，整个实验没有访问真实网络。")


if __name__ == "__main__":
    main()
