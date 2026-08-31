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
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract


def build_agent(tmp_path, allowed_tools=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        FakeModelClient([ModelAction.final("Done.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            allowed_tools=allowed_tools,
            verification_command="",
        ),
    )


def test_allowed_tools_filter_prompt_and_execution(tmp_path):
    agent = build_agent(tmp_path, ["read_file"])
    agent.prompt.build("Read")
    assert [tool["name"] for tool in agent.tools.action_schemas] == [
        "read_file",
        "submit_final",
    ]
    outcome = agent.tools.execute("list_files", {"path": "."})
    assert outcome.status == "rejected"
    assert outcome.failure.code == "tool_not_allowed"


def test_unknown_allowed_tool_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown allowed tool"):
        build_agent(tmp_path, ["missing"])


def test_allowed_tool_schema_order_is_registry_stable(tmp_path):
    first = build_agent(tmp_path / "first", ["edit_file", "read_file"])
    second = build_agent(tmp_path / "second", ["read_file", "edit_file"])

    first_names = [tool["name"] for tool in first.tools.action_schemas]
    second_names = [tool["name"] for tool in second.tools.action_schemas]
    assert first_names == second_names == [
        "read_file",
        "edit_file",
        "submit_final",
    ]


def test_read_only_surface_and_direct_execution_both_reject_mutators(tmp_path):
    agent = build_agent(tmp_path)
    run_log = RunLog(
        "run_read_only",
        "task_read_only",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    first = run_log.append_user(
        TaskContract(
            goal="Inspect",
            task_kind="read_only",
            requires_workspace_change=False,
            requires_verification=False,
        )
    )
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.run_log = run_log
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    executed = []
    agent.tools.registry["write_file"]["run"] = lambda _args: executed.append(True)

    visible = {tool["name"] for tool in agent.tools.model_action_tools()}
    call = ToolCall(
        "write_file",
        {"path": "forbidden.txt", "content": "forbidden\n"},
        "call_forbidden",
    )
    agent.apply_run_event(run_log.append_tool_call(call))
    outcome = agent.tools.execute(call)

    assert "read_file" in visible
    assert "submit_final" in visible
    assert "write_file" not in visible
    assert "edit_file" not in visible
    assert outcome.status == "rejected"
    assert outcome.execution_state == "not_started"
    assert outcome.failure.code == "read_only_task"
    assert executed == []
    result = run_log.events[-1]
    assert result.kind == "tool_result"
    assert result.call_id == call.call_id
    assert result.payload["outcome"]["execution_state"] == "not_started"
    assert not (tmp_path / "forbidden.txt").exists()
