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

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert "task_state" not in report
    assert report["project_memory"] == {"count": 0}
    assert report["event_summary"]["event_counts"]["run_finished"] == 1
    assert not {
        "run_id", "task_id", "status", "stop_reason", "attempts",
        "tool_steps", "last_tool", "checkpoint_id",
    } & set(report["event_summary"])
    agent.evidence_ledger.observations.append({"tool": "invented"})
    rebuilt = agent.build_report(agent.current_task_state)
    assert rebuilt["evidence"] == report["evidence"]

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


def test_tool_turn_reuses_initial_prompt_and_records_provider_result(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.final("Done."),
        ],
    )

    assert agent.ask("Inspect hello") == "Done."
    assert len(agent.model_client.prompts) == 2
    assert agent.model_client.prompts[0] == agent.model_client.prompts[1]
    assert agent.model_client.recorded_action_results[0][0] == "tool"
    assert "alpha" in agent.model_client.recorded_action_results[0][1]
    prompt_events = [
        event for event in agent.run_store.read_events(agent.current_task_state)
        if event["event_type"] == "prompt_built"
    ]
    assert prompt_events[0]["payload"]["prompt_metadata"]["prompt_reused"] is False
    assert prompt_events[1]["payload"]["prompt_metadata"]["prompt_reused"] is True


def test_provider_session_resets_at_input_threshold_and_persists_decision(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")

    class ThresholdClient(FakeModelClient):
        def complete_action(self, *args, **kwargs):
            action = super().complete_action(*args, **kwargs)
            self.last_completion_metadata = {"input_tokens": 7900}
            return action

    client = ThresholdClient([
        ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
        ModelAction.final("Done after reset."),
    ])
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        approval_policy="auto",
        verification_command="",
        provider_context_limit_tokens=8000,
    )

    assert agent.ask("Inspect hello") == "Done after reset."
    assert client.prompts[0] != client.prompts[1]
    assert "alpha" in client.prompts[1]
    events = agent.run_store.read_events(agent.current_task_state)
    reset = next(event for event in events if event["event_type"] == "provider_session_reset")
    assert reset["payload"]["reason"] == "input_threshold"
    assert reset["payload"]["input_tokens"] == 7900


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


def test_default_loop_has_no_tool_step_limit(tmp_path):
    (tmp_path / "many.txt").write_text(
        "".join(f"line-{index}\n" for index in range(1, 9)),
        encoding="utf-8",
    )
    outputs = [
        ModelAction.tool("read_file", {"path": "many.txt", "start": index, "end": index})
        for index in range(1, 8)
    ]
    outputs.append(ModelAction.final("Completed seven reads."))
    agent = build_agent(tmp_path, outputs)

    answer = agent.ask("Read seven distinct lines")

    assert answer == "Completed seven reads."
    assert agent.max_steps is None
    assert agent.current_task_state.tool_steps == 7


def test_next_run_receives_summary_without_prior_tool_transcript(tmp_path):
    (tmp_path / "hello.txt").write_text("unique-tool-output\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.final("First run completed."),
            ModelAction.final("Second run completed."),
        ],
    )

    assert agent.ask("Inspect hello.txt") == "First run completed."
    assert agent.ask("Summarize the prior run") == "Second run completed."

    second_run_prompt = agent.model_client.prompts[2]
    assert "[prior/run_summary] request: Inspect hello.txt" in second_run_prompt
    assert "result: First run completed." in second_run_prompt
    assert "[prior/tool" not in second_run_prompt
    assert [item["role"] for item in agent.session["history"]] == [
        "run_summary",
        "run_summary",
    ]


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
