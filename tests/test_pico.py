import json
from unittest.mock import patch

import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.contracts import ToolCall
from pico.providers.clients import OpenAICompatibleModelClient, _action_from_response
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract

READ_TASK = {
    "task_kind": "read_only",
    "requires_workspace_change": False,
    "requires_verification": False,
}
NO_CHANGE_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": False,
    "requires_verification": False,
}


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n")
    return Pico(
        FakeModelClient(outputs), WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto", verification_command="", **kwargs
        ),
    )


def test_native_tool_loop_records_context_and_working_goal(tmp_path):
    agent = build_agent(tmp_path, [
        ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 2}),
        ModelAction.final("Read successfully."),
    ])
    assert agent.ask("Read hello", **READ_TASK) == "Read successfully."
    assert [entry.kind for entry in agent.run.run_log.context_events()] == [
        "user_message", "assistant_tool_call", "tool_result", "assistant_final"
    ]
    assert agent.run.task.contract.goal == "Read hello"
    assert agent.run.task.lifecycle.status == "completed"
    assert agent.dependencies.run_store is not None
    assert not hasattr(agent, "services")


def test_memory_and_repo_map_remain_enabled_by_default(tmp_path):
    agent = build_agent(tmp_path, [])
    tool_names = {tool["name"] for tool in agent.tools.action_schemas}

    assert agent.dependencies.project_memory is not None
    assert agent.dependencies.repo_map is not None
    assert {"memory_recall", "memory_store", "memory_forget"} <= tool_names


def test_emit_event_requires_one_consistent_active_run(tmp_path):
    agent = build_agent(tmp_path, [])
    with pytest.raises(RuntimeError, match="active TaskState and RunLog"):
        agent.emit_event("model_requested")

    active_log = RunLog(
        "run_active",
        "task_active",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    first = active_log.append_user(TaskContract(goal="Inspect", **READ_TASK))
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.run_log = RunLog(
        "run_other",
        "task_active",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    with pytest.raises(RuntimeError, match="different Runs"):
        agent.emit_event("model_requested")


def test_working_state_tool_is_durable_and_replayable(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool(
                "update_working_state",
                {
                    "add_constraints": ["Keep Python 3.10 compatibility"],
                    "add_decisions": ["The timeout is in token refresh"],
                    "add_next_steps": ["Add a concurrent refresh test"],
                },
            ),
            ModelAction.final("Working state recorded."),
        ],
    )

    assert agent.ask("Fix the login timeout", **NO_CHANGE_TASK) == "Working state recorded."
    state = agent.run.task.working
    assert agent.run.task.contract.goal == "Fix the login timeout"
    assert state.constraints == ("Keep Python 3.10 compatibility",)
    assert state.decisions == ("The timeout is in token refresh",)
    assert state.next_steps == ("Add a concurrent refresh test",)
    replayed = agent.dependencies.run_store.replay(agent.run.projection.run_id)
    assert replayed.task.working.to_dict() == state.to_dict()


def test_rejected_working_state_update_does_not_change_projection(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool(
                "update_working_state",
                {
                    "add_constraints": ["Keep the public API"],
                    "remove_constraints": ["Keep the public API"],
                },
            ),
            ModelAction.final("Rejected update left state unchanged."),
        ],
    )

    assert agent.ask("Inspect the API", **NO_CHANGE_TASK) == "Rejected update left state unchanged."
    assert agent.run.task.working.constraints == ()
    results = [
        event
        for event in agent.run.run_log.events
        if event.kind == "tool_result"
    ]
    assert results[0].outcome_status == "rejected"


def test_fake_client_refuses_legacy_text_protocol(tmp_path):
    agent = build_agent(tmp_path, ["legacy text"])
    with pytest.raises(TypeError, match="ModelAction"):
        agent.ask("do it", **NO_CHANGE_TASK)


def test_ask_requires_explicit_task_requirements(tmp_path):
    agent = build_agent(tmp_path, [ModelAction.final("unused")])

    with pytest.raises(TypeError, match="task_kind"):
        agent.ask("legacy untyped task")


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
    assert _action_from_response({"output": []}, tools).kind == "invalid"


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
        action = client.complete_action(
            "prompt", 32, instructions="runtime rules", action_tools=tools
        )
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
        action = client.complete_action(
            "prompt", 32, instructions="runtime rules", action_tools=tools
        )
    assert action == ModelAction.final("stream ok")


def test_revision_conflict_is_a_tool_error(tmp_path):
    agent = build_agent(tmp_path, [])
    RunLifecycle(agent).initialize("Edit hello", **NO_CHANGE_TASK)
    read_call = ToolCall("read_file", {"path": "hello.txt"}, "read")
    agent.apply_run_event(agent.run.run_log.append_tool_call(read_call))
    read = agent.tools.run(read_call)
    revision = read.content.split("revision: ", 1)[1].splitlines()[0]
    (tmp_path / "hello.txt").write_text("external\n")
    edit_call = ToolCall("edit_file", {
        "path": "hello.txt", "old_text": "external", "new_text": "lost",
        "expected_revision": revision,
    }, "patch")
    agent.apply_run_event(agent.run.run_log.append_tool_call(edit_call))
    outcome = agent.tools.run(edit_call)
    assert outcome.status == "error"
    assert "revision conflict" in outcome.content


def test_session_schema_is_strict_not_migrated(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session.data["schema_version"] = "old"
    with pytest.raises(ValueError, match="unsupported session schema"):
        Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
             agent.session.store, session=agent.session.data,
             config=PicoConfig(verification_command=""))


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
        config=PicoConfig(verification_command=""),
    )

    agent.prompt.refresh(force=True)

    assert agent.workspace.context.repo_root == str(workspace_root.resolve())
    assert agent.workspace.context.cwd == str(invocation_cwd.resolve())


def test_reset_terminalizes_interrupted_run_before_starting_a_new_task(tmp_path):
    agent = build_agent(tmp_path, [])
    with pytest.raises(RuntimeError, match="ran out of outputs"):
        agent.ask("old task", **NO_CHANGE_TASK)
    old_run_id = agent.run.projection.run_id

    agent.reset()
    agent.model_client.outputs.append(ModelAction.final("new answer"))

    assert agent.ask("new task", **NO_CHANGE_TASK) == "new answer"
    assert agent.run.projection.run_id != old_run_id
    assert agent.run.task.contract.goal == "new task"
    old_projection = agent.dependencies.run_store.replay(old_run_id)
    assert old_projection.terminal is True
    assert old_projection.stop_reason == "user_reset"


def test_reset_applies_terminal_event_before_session_persistence(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [])
    with pytest.raises(RuntimeError, match="ran out of outputs"):
        agent.ask("old task", **NO_CHANGE_TASK)

    def fail_session_reset():
        raise OSError("session persistence failed")

    monkeypatch.setattr(agent.session, "reset", fail_session_reset)

    with pytest.raises(OSError, match="session persistence failed"):
        agent.reset()

    assert agent.run.task.lifecycle.status == "stopped"
    assert agent.run.task.lifecycle.stop_reason == "user_reset"
    assert (
        agent.dependencies.run_store.replay(agent.run.projection.run_id).task.to_dict()
        == agent.run.task.to_dict()
    )


def test_terminal_run_closes_execution_when_session_pointer_save_fails(
    tmp_path, monkeypatch
):
    agent = build_agent(tmp_path, [ModelAction.final("Done.")])
    original_set_active_run = agent.session.set_active_run

    def fail_terminal_pointer(run_id):
        if str(run_id) == "":
            raise OSError("terminal pointer failed")
        return original_set_active_run(run_id)

    monkeypatch.setattr(agent.session, "set_active_run", fail_terminal_pointer)

    with pytest.raises(OSError, match="terminal pointer failed"):
        agent.ask("Finish", **NO_CHANGE_TASK)

    assert agent.run.task.lifecycle.status == "completed"
    assert agent.run.execution_context is None
    assert agent.dependencies.run_store.replay(agent.run.projection.run_id).terminal


def test_custom_instructions_rebuild_their_cache_hash(tmp_path):
    agent = build_agent(tmp_path, [])
    original_hash = agent.prompt.instructions_state.content_hash

    agent.prompt.instructions = "custom interview rules"
    prompt, metadata = agent.prompt.build("inspect")

    assert prompt.instructions == "custom interview rules"
    assert "custom interview rules" not in prompt.input_text
    assert agent.prompt.instructions_state.content_hash != original_hash
    assert metadata["prompt_cache_key"] == agent.prompt.instructions_state.content_hash
