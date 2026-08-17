import json
from unittest.mock import patch

import pytest

from pico import FakeModelClient, ModelAction, Pico, SessionStore, WorkspaceContext
from pico.contracts import ToolCall
from pico.providers.clients import OpenAICompatibleModelClient, _action_from_response


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n")
    return Pico(
        FakeModelClient(outputs), WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"), approval_policy="auto",
        verification_command="", **kwargs,
    )


def test_native_tool_loop_records_context_and_memory(tmp_path):
    agent = build_agent(tmp_path, [
        ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 2}),
        ModelAction.final("Read successfully."),
    ])
    assert agent.ask("Read hello") == "Read successfully."
    assert [entry.kind for entry in agent.context_ledger.entries] == [
        "user", "assistant_tool_call", "tool_result", "final"
    ]
    assert "hello.txt" in agent.memory.render_panel()
    assert agent.current_task_state.status == "completed"


def test_fake_client_refuses_legacy_text_protocol(tmp_path):
    agent = build_agent(tmp_path, ["legacy text"])
    with pytest.raises(TypeError, match="ModelAction"):
        agent.ask("do it")


def test_response_action_parser_requires_one_allowed_function():
    tools = [{"name": "read_file"}, {"name": "submit_final"}]
    action = _action_from_response({
        "output": [{"type": "function_call", "name": "read_file",
                    "call_id": "c1", "arguments": '{"path":"a.txt"}'}]
    }, tools)
    assert action.kind == "tool"
    assert action.tool_call.call_id == "c1"
    final = _action_from_response({
        "output": [{"type": "function_call", "name": "submit_final",
                    "arguments": {"answer": "done"}}]
    }, tools)
    assert final == ModelAction.final("done")
    assert _action_from_response({"output": []}, tools).kind == "retry"


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


def test_openai_client_sends_strict_native_tools_and_parses_action(tmp_path):
    client = OpenAICompatibleModelClient("gpt-test", "https://example.test/v1", "key", 0, 10)
    tools = [{"type": "function", "name": "submit_final", "description": "final",
              "parameters": {"type": "object"}, "strict": True}]
    response = {"output": [{"type": "function_call", "name": "submit_final",
                            "arguments": '{"answer":"ok"}'}],
                "usage": {"input_tokens": 3, "output_tokens": 2}}
    captured = {}

    def urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response(json.dumps(response))

    with patch("urllib.request.urlopen", urlopen):
        action = client.complete_action("prompt", 32, action_tools=tools)
    assert action == ModelAction.final("ok")
    assert captured["payload"]["tool_choice"] == "required"
    assert captured["payload"]["parallel_tool_calls"] is False
    assert captured["payload"]["tools"] == tools
    assert client.last_completion_metadata["input_tokens"] == 3


def test_openai_sse_completed_function_call_is_parsed():
    client = OpenAICompatibleModelClient("gpt-test", "https://example.test/v1", "", None, 10)
    tools = [{"name": "submit_final"}]
    event = {"type": "response.completed", "response": {
        "output": [{"type": "function_call", "name": "submit_final",
                    "arguments": '{"answer":"stream ok"}'}]}}
    with patch("urllib.request.urlopen", return_value=Response(
        "data: " + json.dumps(event) + "\n\ndata: [DONE]\n", "text/event-stream"
    )):
        action = client.complete_action("prompt", 32, action_tools=tools)
    assert action == ModelAction.final("stream ok")


def test_revision_conflict_is_a_tool_error(tmp_path):
    agent = build_agent(tmp_path, [])
    read = agent.run_tool(ToolCall("read_file", {"path": "hello.txt"}, "read"))
    revision = read.content.split("revision: ", 1)[1].splitlines()[0]
    (tmp_path / "hello.txt").write_text("external\n")
    outcome = agent.run_tool(ToolCall("patch_file", {
        "path": "hello.txt", "old_text": "external", "new_text": "lost",
        "expected_revision": revision,
    }, "patch"))
    assert outcome.status == "error"
    assert "revision conflict" in outcome.content


def test_session_schema_is_strict_not_migrated(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["schema_version"] = "old"
    with pytest.raises(ValueError, match="unsupported session schema"):
        Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
             agent.session_store, session=agent.session, verification_command="")


def test_prefix_refresh_preserves_explicit_workspace_root_and_invocation_cwd(tmp_path):
    workspace_root = tmp_path / "fixture"
    invocation_cwd = workspace_root / "src"
    invocation_cwd.mkdir(parents=True)
    (workspace_root / "README.md").write_text("fixture\n", encoding="utf-8")
    workspace = WorkspaceContext.build(invocation_cwd, repo_root_override=workspace_root)
    agent = Pico(
        FakeModelClient([]),
        workspace,
        SessionStore(workspace_root / ".pico" / "sessions"),
        verification_command="",
    )

    agent.refresh_prefix(force=True)

    assert agent.workspace.repo_root == str(workspace_root.resolve())
    assert agent.workspace.cwd == str(invocation_cwd.resolve())
