from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.agent_loop import AgentLoop
from pico.hooks import HookDirective


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        config=PicoConfig(approval_policy="auto"),
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
    assert agent.run.task_state.status == "completed"

    report = agent.build_report(agent.run.task_state)
    assert "task_state" not in report
    assert report["project_memory"] == {"count": 0}
    assert report["journal_summary"]["kind_counts"]["assistant_final"] == 1
    assert report["journal_summary"]["stop_reason"] == "final_answer_returned"
    agent.run.evidence.observations.append({"tool": "invented"})
    rebuilt = agent.build_report(agent.run.task_state)
    assert rebuilt["evidence"] == report["evidence"]

    entries = agent.services.run_store.read_entries(agent.run.task_state)
    assert [entry.sequence for entry in entries] == list(range(1, len(entries) + 1))
    tool_entry = next(entry for entry in entries if entry.kind == "tool_result")
    outcome = tool_entry.payload["outcome"]
    assert outcome["artifact"]["artifact_id"]


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
    turns = [
        entry for entry in agent.services.run_store.read_entries(agent.run.task_state)
        if entry.kind == "turn_metrics"
    ]
    assert [entry.payload["prompt_reused"] for entry in turns] == [False, True]


def test_provider_session_resets_for_complete_next_input_estimate(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")

    class ThresholdClient(FakeModelClient):
        request_count = 0

        def complete_action(self, *args, **kwargs):
            action = super().complete_action(*args, **kwargs)
            self.request_count += 1
            self.last_completion_metadata = (
                {"input_tokens": 6800, "output_tokens": 300}
                if self.request_count == 1
                else {"input_tokens": 1000, "output_tokens": 50}
            )
            return action

    client = ThresholdClient([
        ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
        ModelAction.tool("list_files", {"path": "."}),
        ModelAction.final("Done after reset."),
    ])
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="",
            provider_context_limit_tokens=8000,
        ),
    )
    original_count = agent.prompt.context.tokenizer.count
    agent.prompt.context.tokenizer.count = lambda text: (
        200 if "alpha" in str(text) else original_count(text)
    )

    assert agent.ask("Inspect hello") == "Done after reset."
    assert client.prompts[0] != client.prompts[1]
    assert client.prompts[1] == client.prompts[2]
    assert "alpha" in client.prompts[1]
    entries = agent.services.run_store.read_entries(agent.run.task_state)
    resets = [
        entry for entry in entries if entry.kind == "provider_session_reset"
    ]
    assert len(resets) == 1
    reset = resets[0]
    assert reset.payload == {
        "reason": "next_input_threshold",
        "input_tokens": 6800,
        "output_tokens": 300,
        "tool_result_tokens": 200,
        "estimated_next_total": 8324,
        "provider_context_tokens": 7300,
        "tool_call_id": reset.payload["tool_call_id"],
    }
    turns = [
        entry for entry in entries if entry.kind == "turn_metrics"
    ]
    assert [
        entry.payload["prompt_reused"] for entry in turns
    ] == [False, False, True]
    assert len({
        entry.payload["prompt_metadata"]["prefix_hash"] for entry in turns
    }) == 1


def test_provider_session_continues_when_complete_next_input_fits(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")

    class CapacityClient(FakeModelClient):
        def complete_action(self, *args, **kwargs):
            action = super().complete_action(*args, **kwargs)
            self.last_completion_metadata = {
                "input_tokens": 6000,
                "output_tokens": 300,
            }
            return action

    client = CapacityClient([
        ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
        ModelAction.final("Done without reset."),
    ])
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="",
            provider_context_limit_tokens=8000,
        ),
    )
    original_count = agent.prompt.context.tokenizer.count
    agent.prompt.context.tokenizer.count = lambda text: (
        200 if "alpha" in str(text) else original_count(text)
    )

    assert agent.ask("Inspect hello") == "Done without reset."
    assert client.prompts[0] == client.prompts[1]
    entries = agent.services.run_store.read_entries(agent.run.task_state)
    assert not any(
        entry.kind == "provider_session_reset" for entry in entries
    )


def test_provider_capacity_estimate_counts_runtime_guidance(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")

    class GuidanceClient(FakeModelClient):
        def complete_action(self, *args, **kwargs):
            action = super().complete_action(*args, **kwargs)
            self.last_completion_metadata = {
                "input_tokens": 6800,
                "output_tokens": 0,
            }
            return action

    class GuideAfterRead:
        def after_tool_result(self, context):
            return HookDirective(guidance="Inspect the registry next.")

    client = GuidanceClient([
        ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
        ModelAction.final("Done after guided reset."),
    ])
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="",
            provider_context_limit_tokens=8000,
        ),
        hooks=[GuideAfterRead()],
    )
    original_count = agent.prompt.context.tokenizer.count
    agent.prompt.context.tokenizer.count = lambda text: (
        300 if "Runtime guidance:" in str(text) else original_count(text)
    )

    assert agent.ask("Inspect hello") == "Done after guided reset."
    entries = agent.services.run_store.read_entries(agent.run.task_state)
    reset = next(
        entry for entry in entries if entry.kind == "provider_session_reset"
    )
    assert reset.payload["tool_result_tokens"] == 300
    assert reset.payload["estimated_next_total"] == 8124
    assert reset.payload["provider_context_tokens"] == 7100
    assert "Inspect the registry next." in client.prompts[1]


def test_context_overflow_compacts_and_retries_once(tmp_path):
    for name in ("first.txt", "second.txt"):
        (tmp_path / name).write_text((name + " x" * 200 + "\n") * 80)

    class OverflowClient(FakeModelClient):
        request_count = 0

        def complete_action(self, *args, **kwargs):
            self.request_count += 1
            if self.request_count == 3:
                raise RuntimeError("maximum context length exceeded")
            return super().complete_action(*args, **kwargs)

        def complete(self, prompt, max_new_tokens, **kwargs):
            if kwargs.get("action_tools", object()) is None:
                return "Compacted earlier reads."
            return super().complete(prompt, max_new_tokens, **kwargs)

    client = OverflowClient(
        [
            ModelAction.tool(
                "read_file", {"path": "first.txt", "start": 1, "end": 80}
            ),
            ModelAction.tool(
                "read_file", {"path": "second.txt", "start": 1, "end": 80}
            ),
            ModelAction.final("Recovered after compaction."),
        ]
    )
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="",
            max_new_tokens=64,
            provider_context_limit_tokens=3_000,
            compaction_reserve_tokens=750,
            compaction_keep_recent_tokens=100,
        ),
    )

    assert agent.ask("Read both files and finish") == "Recovered after compaction."
    entries = agent.services.run_store.read_entries(agent.run.task_state)
    assert sum(entry.kind == "compaction" for entry in entries) == 1
    resets = [entry for entry in entries if entry.kind == "provider_session_reset"]
    assert [entry.payload["reason"] for entry in resets] == [
        "context_overflow_retry"
    ]
    assert client.request_count == 4


def test_last_tool_step_gets_one_final_only_model_turn(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.final("Done at the tool boundary."),
        ],
    )
    agent.config = PicoConfig.build(agent.config, max_steps=1)

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Done at the tool boundary."
    assert agent.run.task_state.tool_steps == 1
    assert agent.run.task_state.status == "completed"
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
    assert agent.config.max_steps is None
    assert agent.run.task_state.tool_steps == 7


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
    assert agent.session.data["active_run_id"] == ""


def test_final_only_turn_does_not_execute_an_extra_tool(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start": 1, "end": 1}),
            ModelAction.tool("list_files", {"path": "."}),
        ],
    )
    agent.config = PicoConfig.build(agent.config, max_steps=1)

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Stopped after reaching the step limit without a final answer."
    assert agent.run.task_state.tool_steps == 1
    finished_tools = [
        entry.payload["tool_name"]
        for entry in agent.services.run_store.read_entries(agent.run.task_state)
        if entry.kind == "tool_result"
        and entry.payload["outcome"]["execution_state"] != "not_started"
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
    agent.config = PicoConfig.build(agent.config, max_steps=1)

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after correcting the call."
    assert agent.run.task_state.tool_steps == 1
    assert agent.run.task_state.attempts == 3
    results = [
        entry
        for entry in agent.services.run_store.read_entries(agent.run.task_state)
        if entry.kind == "tool_result"
    ]
    assert sum(entry.payload["outcome"]["status"] == "rejected" for entry in results) == 1
    assert sum(entry.payload["outcome"]["execution_state"] == "completed" for entry in results) == 1


def test_project_memory_selection_is_written_to_journal(tmp_path):
    agent = build_agent(tmp_path, [ModelAction.final("staging")])
    agent.services.project_memory.store(
        action="create",
        filename="project_deploy.md",
        name="Deploy target",
        description="Stable deployment target.",
        memory_type="project",
        content="deploy target is staging",
        why="Deploy commands require the correct environment.",
        how_to_apply="Use staging unless the user overrides it.",
        source_session_id=agent.session.data["id"],
        source_run_id="bootstrap",
    )
    agent.model_client.select_memory_filenames = lambda *args, **kwargs: ["project_deploy.md"]

    assert agent.ask("What is the deploy target?") == "staging"
    entries = agent.services.run_store.read_entries(agent.run.task_state)
    memory_entry = next(entry for entry in entries if entry.kind == "memory_selection")
    assert memory_entry.payload["selected_filenames"] == ["project_deploy.md"]


def test_custom_memory_selector_cannot_escape_the_manifest(tmp_path):
    agent = build_agent(tmp_path, [ModelAction.final("Done.")])
    agent.services.project_memory.store(
        action="create",
        filename="project_available.md",
        name="Available",
        description="Available memory.",
        memory_type="project",
        content="available",
        why="test",
        how_to_apply="test",
        source_session_id=agent.session.data["id"],
        source_run_id="bootstrap",
    )
    agent.model_client.select_memory_filenames = lambda *_args, **_kwargs: [
        "project_unavailable.md"
    ]

    assert agent.ask("Inspect memory") == "Done."
    selection = next(
        entry
        for entry in agent.run.journal.entries
        if entry.kind == "memory_selection"
    )
    assert selection.payload["status"] == "unavailable"
    assert selection.payload["selected_filenames"] == []
