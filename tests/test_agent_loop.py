import json

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.agent_loop import AgentLoop
from pico.sandbox import SandboxResult


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
            ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
            ModelAction.final("Done."),
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.run.task_state.status == "completed"

    entries = agent.dependencies.run_store.read_events(agent.run.task_state)
    assert [entry.sequence for entry in entries] == list(range(1, len(entries) + 1))
    tool_entry = next(entry for entry in entries if entry.kind == "tool_result")
    outcome = tool_entry.payload["outcome"]
    assert outcome["artifact"] == {}


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, [ModelAction.final("Facade works.")])

    assert agent.ask("Use facade") == "Facade works."


def test_invalid_model_outputs_stop_at_the_explicit_limit(tmp_path):
    agent = build_agent(
        tmp_path,
        [ModelAction.invalid("Return one valid action.") for _ in range(8)],
    )

    answer = agent.ask("Inspect the repository")

    assert answer == (
        "Stopped after too many invalid model outputs without a valid tool call "
        "or final answer."
    )
    assert agent.run.task_state.stop_reason == "invalid_output_limit"
    assert agent.run.task_state.model_request_count == 8


def test_repeated_rejected_completion_attempts_stop_at_limit(tmp_path):
    class FailingSandbox:
        @staticmethod
        def run(*_args, **_kwargs):
            return SandboxResult(returncode=1, stderr="assertion failed")

    client = FakeModelClient(
        [
            ModelAction.tool(
                "write_file",
                {
                    "path": "subject.txt",
                    "content": "changed\n",
                    "expected_revision": "absent",
                },
            ),
            ModelAction.final("done"),
            ModelAction.final("done"),
            ModelAction.final("done"),
        ]
    )
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="verify",
        ),
        sandbox=FailingSandbox(),
    )

    answer = agent.ask("Create subject.txt")

    assert answer == "Stopped after repeated rejected completion attempts."
    assert agent.run.task_state.stop_reason == "completion_block_limit"
    events = agent.dependencies.run_store.read_events(agent.run.task_state)
    assert sum(entry.kind == "completion_blocked" for entry in events) == 3


def test_tool_turn_reuses_initial_prompt_and_records_provider_result(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
            ModelAction.final("Done."),
        ],
    )

    assert agent.ask("Inspect hello") == "Done."
    assert len(agent.model_client.prompts) == 2
    assert agent.model_client.prompts[0] == agent.model_client.prompts[1]
    assert agent.model_client.recorded_action_results[0][0] == "tool"
    result = json.loads(agent.model_client.recorded_action_results[0][1])
    assert result["status"] == "success"
    assert result["correction_action"] == "continue"
    assert result["structured"]["path"] == "hello.txt"
    assert "alpha" in result["content"]
    turns = [
        entry for entry in agent.dependencies.run_store.read_events(agent.run.task_state)
        if entry.kind == "turn_metrics"
    ]
    assert [entry.payload["prompt_reused"] for entry in turns] == [False, True]
    assert "sections" in turns[0].payload["prompt_metadata"]
    assert "sections" not in turns[1].payload["prompt_metadata"]
    assert len(str(turns[1].payload["prompt_metadata"])) < len(
        str(turns[0].payload["prompt_metadata"])
    )


def test_provider_session_resets_for_complete_next_input_estimate(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")

    class ThresholdClient(FakeModelClient):
        request_count = 0

        def complete_action(self, *args, **kwargs):
            action = super().complete_action(*args, **kwargs)
            self.request_count += 1
            self.last_completion_metadata = (
                {"input_tokens": 6500, "output_tokens": 300}
                if self.request_count == 1
                else {"input_tokens": 1000, "output_tokens": 50}
            )
            return action

    client = ThresholdClient([
        ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
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
            compaction_reserve_tokens=2000,
            compaction_keep_recent_tokens=6000,
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
    entries = agent.dependencies.run_store.read_events(agent.run.task_state)
    resets = [
        entry for entry in entries if entry.kind == "provider_session_reset"
    ]
    assert len(resets) == 1
    reset = resets[0]
    assert reset.payload == {
        "reason": "next_input_threshold",
        "input_tokens": 6500,
        "output_tokens": 300,
        "tool_result_tokens": 200,
        "estimated_next_total": 8024,
        "provider_context_tokens": 7000,
        "tool_call_id": reset.payload["tool_call_id"],
    }
    turns = [
        entry for entry in entries if entry.kind == "turn_metrics"
    ]
    assert [
        entry.payload["prompt_reused"] for entry in turns
    ] == [False, False, True]
    assert len({
        entry.payload["prompt_metadata"]["prompt_cache_key"] for entry in turns
    }) == 1
    first_prompt = turns[0].payload["prompt_metadata"]
    observed_overhead = 6500 - first_prompt["prompt_tokens"]
    assert first_prompt["observed_provider_overhead_tokens"] == observed_overhead
    assert turns[1].payload["prompt_metadata"]["provider_overhead_tokens"] == observed_overhead


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
        ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
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
            compaction_reserve_tokens=2000,
            compaction_keep_recent_tokens=6000,
        ),
    )
    original_count = agent.prompt.context.tokenizer.count
    agent.prompt.context.tokenizer.count = lambda text: (
        200 if "alpha" in str(text) else original_count(text)
    )

    assert agent.ask("Inspect hello") == "Done without reset."
    assert client.prompts[0] == client.prompts[1]
    entries = agent.dependencies.run_store.read_events(agent.run.task_state)
    assert not any(
        entry.kind == "provider_session_reset" for entry in entries
    )


def test_provider_capacity_estimate_counts_budget_instruction(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")

    class GuidanceClient(FakeModelClient):
        def complete_action(self, *args, **kwargs):
            action = super().complete_action(*args, **kwargs)
            self.last_completion_metadata = {
                "input_tokens": 6700,
                "output_tokens": 0,
            }
            return action

    client = GuidanceClient([
        ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
        ModelAction.final("Done after guided reset."),
    ])
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            max_tool_executions=1,
            verification_command="",
            provider_context_limit_tokens=8000,
            compaction_reserve_tokens=2000,
            compaction_keep_recent_tokens=6000,
        ),
    )
    original_count = agent.prompt.context.tokenizer.count
    agent.prompt.context.tokenizer.count = lambda text: (
        300 if "Runtime instruction:" in str(text) else original_count(text)
    )

    assert agent.ask("Inspect hello") == "Done after guided reset."
    entries = agent.dependencies.run_store.read_events(agent.run.task_state)
    reset = next(
        entry for entry in entries if entry.kind == "provider_session_reset"
    )
    assert reset.payload["tool_result_tokens"] == 300
    assert reset.payload["estimated_next_total"] == 8024
    assert reset.payload["provider_context_tokens"] == 7000
    assert "Runtime tool budget exhausted" in client.prompts[1]


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
                "read_file", {"path": "first.txt", "start_line": 1, "end_line": 80}
            ),
            ModelAction.tool(
                "read_file", {"path": "second.txt", "start_line": 1, "end_line": 80}
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
    entries = agent.dependencies.run_store.read_events(agent.run.task_state)
    assert sum(entry.kind == "compaction" for entry in entries) == 1
    resets = [entry for entry in entries if entry.kind == "provider_session_reset"]
    assert [entry.payload["reason"] for entry in resets] == [
        "context_overflow_retry"
    ]
    assert client.request_count == 4


def test_tool_execution_at_limit_gets_one_final_only_model_turn(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
            ModelAction.final("Done at the tool boundary."),
        ],
    )
    agent.config = PicoConfig.build(agent.config, max_tool_executions=1)

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Done at the tool boundary."
    assert agent.run.task_state.executed_tool_count == 1
    assert agent.run.task_state.status == "completed"
    assert agent.model_client.action_tool_surfaces[-1] == ("submit_final",)


def test_default_loop_has_no_tool_execution_limit(tmp_path):
    (tmp_path / "many.txt").write_text(
        "".join(f"line-{index}\n" for index in range(1, 9)),
        encoding="utf-8",
    )
    outputs = [
        ModelAction.tool("read_file", {"path": "many.txt", "start_line": index, "end_line": index})
        for index in range(1, 8)
    ]
    outputs.append(ModelAction.final("Completed seven reads."))
    agent = build_agent(tmp_path, outputs)

    answer = agent.ask("Read seven distinct lines")

    assert answer == "Completed seven reads."
    assert agent.config.max_tool_executions is None
    assert agent.run.task_state.executed_tool_count == 7


def test_next_run_does_not_implicitly_receive_prior_run_context(tmp_path):
    (tmp_path / "hello.txt").write_text("unique-tool-output\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
            ModelAction.final("First run completed."),
            ModelAction.final("Second run completed."),
        ],
    )

    assert agent.ask("Inspect hello.txt") == "First run completed."
    assert agent.ask("Summarize the prior run") == "Second run completed."

    second_run_prompt = agent.model_client.prompts[2]
    assert "Inspect hello.txt" not in second_run_prompt
    assert "First run completed." not in second_run_prompt
    assert "unique-tool-output" not in second_run_prompt
    assert "Current run events:\n- empty" in second_run_prompt
    assert second_run_prompt.endswith("Current user request:\nSummarize the prior run")
    assert agent.session.data["active_run_id"] == ""


def test_final_only_turn_does_not_execute_an_extra_tool(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
            ModelAction.tool("list_files", {"path": "."}),
        ],
    )
    agent.config = PicoConfig.build(agent.config, max_tool_executions=1)

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Stopped after reaching the tool execution limit without a final answer."
    assert agent.run.task_state.executed_tool_count == 1
    finished_tools = [
        entry.payload["tool_name"]
        for entry in agent.dependencies.run_store.read_events(agent.run.task_state)
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
            ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
            ModelAction.final("Recovered after correcting the call."),
        ],
    )
    agent.config = PicoConfig.build(agent.config, max_tool_executions=1)

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after correcting the call."
    assert agent.run.task_state.executed_tool_count == 1
    assert agent.run.task_state.model_request_count == 3
    results = [
        entry
        for entry in agent.dependencies.run_store.read_events(agent.run.task_state)
        if entry.kind == "tool_result"
    ]
    assert sum(entry.payload["outcome"]["status"] == "rejected" for entry in results) == 1
    assert sum(entry.payload["outcome"]["execution_state"] == "completed" for entry in results) == 1


def test_project_memory_recall_is_an_explicit_tool_transaction(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool(
                "memory_recall",
                {"filenames": ["project_deploy.md"]},
            ),
            ModelAction.final("staging"),
        ],
    )
    agent.dependencies.project_memory.store(
        action="create",
        filename="project_deploy.md",
        name="Deploy target",
        description="Stable deployment target.",
        memory_type="project",
        content="deploy target is staging",
        why="Deploy commands require the correct environment.",
        how_to_apply="Use staging unless the user overrides it.",
        source_run_id="bootstrap",
    )
    assert agent.ask("What is the deploy target?") == "staging"
    entries = agent.dependencies.run_store.read_events(agent.run.task_state)
    recall_call = next(
        entry
        for entry in entries
        if entry.kind == "assistant_tool_call" and entry.name == "memory_recall"
    )
    recall_result = next(
        entry
        for entry in entries
        if entry.kind == "tool_result" and entry.call_id == recall_call.call_id
    )
    assert recall_result.outcome_status == "success"
    assert "deploy target is staging" in recall_result.content
    assert agent.run.task_state.model_request_count == 2


def test_memory_recall_rejects_unavailable_filenames(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool(
                "memory_recall",
                {"filenames": ["project_unavailable.md"]},
            ),
            ModelAction.final("Done."),
        ],
    )
    agent.dependencies.project_memory.store(
        action="create",
        filename="project_available.md",
        name="Available",
        description="Available memory.",
        memory_type="project",
        content="available",
        why="test",
        how_to_apply="test",
        source_run_id="bootstrap",
    )
    assert agent.ask("Inspect memory") == "Done."
    result = next(
        entry
        for entry in agent.run.run_log.events
        if entry.kind == "tool_result" and entry.name == "memory_recall"
    )
    assert result.outcome_status == "rejected"
    assert "unavailable filename" in result.content
