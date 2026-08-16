from pico import FakeModelClient, ModelAction, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_agent_loop_runs_same_control_flow_as_pico_ask(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.final("Done."),
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.run_store.report_path(agent.current_task_state.run_id).exists()

    events = agent.run_store.read_events(agent.current_task_state)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["causation_id"] == ""
    assert all(
        event["causation_id"] == events[index - 1]["event_id"]
        for index, event in enumerate(events[1:], start=1)
    )
    tool_event = next(event for event in events if event["event_type"] == "operation_finished")
    outcome = tool_event["payload"]["outcome"]
    assert tool_event["correlation_id"] == outcome["tool_call_id"]
    assert outcome["artifact"]["artifact_id"] == outcome["artifact_id"]


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, [ModelAction.final("Facade works.")])

    assert agent.ask("Use facade") == "Facade works."


def test_last_tool_step_gets_one_final_only_model_turn(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.final("Done at the tool boundary."),
        ],
    )
    agent.max_steps = 1

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Done at the tool boundary."
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.status == "completed"
    assert agent.model_client.action_tool_surfaces[-1] == ("submit_final",)


def test_final_only_turn_does_not_execute_an_extra_tool(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.tool("list_files", {"path": "."}),
        ],
    )
    agent.max_steps = 1

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Stopped after reaching the step limit without a final answer."
    assert agent.current_task_state.tool_steps == 1
    finished_tools = [
        event["payload"]["tool_name"]
        for event in agent.run_store.read_events(agent.current_task_state)
        if event["event_type"] == "operation_finished"
    ]
    assert finished_tools == ["read_file"]


def test_admission_rejection_does_not_consume_execution_budget(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "missing.txt"}),
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.final("Recovered after correcting the call."),
        ],
    )
    agent.max_steps = 1

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after correcting the call."
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.attempts == 3
    events = agent.run_store.read_events(agent.current_task_state)
    assert sum(event["event_type"] == "tool_rejected" for event in events) == 1
    assert sum(event["event_type"] == "operation_finished" for event in events) == 1


def test_project_memory_selection_is_written_to_event_log(tmp_path):
    agent = build_agent(tmp_path, [ModelAction.final("staging")])
    agent.project_memory.store(
        action="create",
        filename="project_deploy.md",
        name="Deploy target",
        description="Stable deployment target.",
        memory_type="project",
        content="deploy target is staging",
        why="Deploy commands require the correct environment.",
        how_to_apply="Use staging unless the user overrides it.",
        origin="explicit",
        source_session_id=agent.session["id"],
        source_run_id="bootstrap",
    )
    agent.model_client.select_memory_filenames = lambda *args, **kwargs: ["project_deploy.md"]

    assert agent.ask("What is the deploy target?") == "staging"
    events = agent.run_store.read_events(agent.current_task_state)
    memory_event = next(event for event in events if event["event_type"] == "memory_selection")
    assert memory_event["payload"]["selected_filenames"] == ["project_deploy.md"]
