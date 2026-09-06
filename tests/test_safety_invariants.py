from pico import FakeModelClient, Pico, PicoConfig, SessionStore, Workspace
from pico.contracts import ToolCall
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.task_state import TaskContract


def build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n")
    runtime_workspace = Workspace.build(tmp_path)
    return Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        **kwargs,
        session=SessionStore(tmp_path / ".pico/sessions").create(
            runtime_workspace.root
        ),
    )


def run_active(agent, call):
    run_log = agent.run.run_log
    if run_log is None:
        run_log = RunLog(
            "run_safety_test",
            "task_safety_test",
            agent.session.id,
            agent.dependencies.run_store,
        )
        run_log.append_user(
            TaskContract(
                goal="Exercise path safety",
                allows_workspace_mutation=True,
                verify_changes=False,
            )
        )
        agent.run.projection = run_log.projection
        agent.run.run_log = run_log
        agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    group = run_log.append_tool_calls((call,))
    return agent.tools.execute_pending_group(group.event_id)[0]


def test_workspace_and_symlink_escape_are_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("secret")
    agent = build_agent(tmp_path)
    assert agent.tools.execute_manual(
        "read_file", {"path": "../" + outside.name}
    ).status == "rejected"
    (tmp_path / "link").symlink_to(outside)
    assert agent.tools.execute_manual("read_file", {"path": "link"}).status == (
        "rejected"
    )


def test_file_tools_reject_git_and_pico_internal_paths(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("internal\n", encoding="utf-8")
    agent = build_agent(tmp_path)

    read_git = agent.tools.execute_manual(
        "read_file",
        {"path": ".git/config", "start_line": 1, "end_line": 10},
    )
    write_pico = run_active(
        agent,
        ToolCall(
            "write_file",
            {"path": ".pico/injected.txt", "content": "injected\n"},
            "call_write_pico",
        ),
    )
    write_gitignore = run_active(
        agent,
        ToolCall(
            "write_file",
            {"path": ".gitignore", "content": ".pico/\n"},
            "call_write_gitignore",
        ),
    )

    assert read_git.status == "rejected"
    assert write_pico.status == "rejected"
    assert not (tmp_path / ".pico" / "injected.txt").exists()
    assert write_gitignore.status == "success"
