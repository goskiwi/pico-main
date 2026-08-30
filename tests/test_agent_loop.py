import json

import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.agent_loop import AgentLoop
from pico.contracts import ToolOutcome
from pico.evidence import verification_is_current
from pico.mutations import content_revision, file_revision
from pico.providers import ProviderContextOverflow
from pico.sandbox import SandboxResult

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
MODIFY_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": True,
    "requires_verification": False,
}
VERIFIED_MODIFY_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": True,
    "requires_verification": True,
}


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

    outcome = AgentLoop(agent).run("Inspect hello.txt", **READ_TASK)

    assert outcome.answer == "Done."
    assert agent.run.task.lifecycle.status == "completed"

    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    assert [entry.sequence for entry in entries] == list(range(1, len(entries) + 1))
    tool_entry = next(entry for entry in entries if entry.kind == "tool_result")
    outcome = tool_entry.payload["outcome"]
    assert outcome["artifact_id"] == ""


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, [ModelAction.final("Facade works.")])

    assert agent.ask("Use facade", **NO_CHANGE_TASK).answer == "Facade works."


def test_stale_edit_conflict_re_reads_repairs_and_verifies_current_workspace(
    tmp_path,
):
    target = tmp_path / "subject.txt"
    target.write_text("alpha\n", encoding="utf-8")
    initial_revision = file_revision(target)
    external_content = "alpha\nexternal\n"
    external_revision = content_revision(external_content.encode("utf-8"))
    verified_contents = []

    class DriftBeforeFirstEditClient(FakeModelClient):
        request_count = 0

        def complete_action(self, *args, **kwargs):
            self.request_count += 1
            if self.request_count == 2:
                target.write_text(external_content, encoding="utf-8")
            return super().complete_action(*args, **kwargs)

    class RecordingVerificationSandbox:
        @staticmethod
        def run(*_args, **_kwargs):
            verified_contents.append(target.read_text(encoding="utf-8"))
            return SandboxResult(returncode=0, stdout="1 passed\n")

    client = DriftBeforeFirstEditClient(
        [
            ModelAction.tool(
                "read_file",
                {"path": "subject.txt", "start_line": 1, "end_line": 20},
                call_id="call_read_initial",
            ),
            ModelAction.tool(
                "edit_file",
                {
                    "path": "subject.txt",
                    "old_text": "alpha\n",
                    "new_text": "agent\n",
                    "expected_revision": initial_revision,
                },
                call_id="call_edit_stale",
            ),
            ModelAction.tool(
                "read_file",
                {"path": "subject.txt", "start_line": 1, "end_line": 20},
                call_id="call_read_current",
            ),
            ModelAction.tool(
                "edit_file",
                {
                    "path": "subject.txt",
                    "old_text": "alpha\n",
                    "new_text": "agent\n",
                    "expected_revision": external_revision,
                },
                call_id="call_edit_repaired",
            ),
            ModelAction.final("Recovered safely."),
        ]
    )
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="verify",
        ),
        sandbox=RecordingVerificationSandbox(),
    )

    outcome = agent.ask(
        "Replace alpha without losing concurrent edits", **VERIFIED_MODIFY_TASK
    )

    assert outcome.answer == "Recovered safely."
    assert target.read_text(encoding="utf-8") == "agent\nexternal\n"
    assert verified_contents == ["agent\nexternal\n"]

    events = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    outcomes = {
        entry.call_id: entry.payload["outcome"]
        for entry in events
        if entry.kind == "tool_result"
    }
    conflict = outcomes["call_edit_stale"]
    assert conflict["status"] == "error"
    assert conflict["execution_state"] == "failed"
    assert conflict["side_effect_state"] == "none"
    assert ToolOutcome.from_dict(conflict).correction_action == "repair"
    assert conflict["failure"]["code"] == "revision_conflict"
    assert conflict["failure"]["recovery"] == "retry_after_change"
    assert conflict["structured"] == {
        "path": "subject.txt",
        "expected_revision": initial_revision,
        "actual_revision": external_revision,
        "recommended_next_tool": "read_file",
    }
    assert outcomes["call_read_current"]["structured"]["revision"] == (
        external_revision
    )
    assert outcomes["call_edit_repaired"]["status"] == "success"
    assert outcomes["call_edit_repaired"]["structured"]["before_revision"] == (
        external_revision
    )

    repaired_sequence = next(
        entry.sequence
        for entry in events
        if entry.kind == "tool_result" and entry.call_id == "call_edit_repaired"
    )
    verification = next(
        entry for entry in events if entry.kind == "verification_result"
    )
    assert verification.sequence > repaired_sequence
    assert verification.payload["status"] == "passed"
    assert "freshness" not in verification.payload
    assert verification_is_current(
        verification.payload,
        agent.run.evidence.last_workspace_mutation_sequence,
        agent.run.evidence.change_set.current_net_path_states,
    )


def test_invalid_model_outputs_stop_at_the_explicit_limit(tmp_path):
    agent = build_agent(
        tmp_path,
        [ModelAction.invalid("Return one valid action.") for _ in range(8)],
    )

    outcome = agent.ask("Inspect the repository", **READ_TASK)

    assert outcome.answer == (
        "Stopped after too many invalid model outputs without a valid tool call "
        "or final answer."
    )
    replayed = agent.dependencies.run_store.replay(outcome.run_id)
    assert outcome.status == replayed.status == "stopped"
    assert outcome.answer == replayed.final_answer
    assert outcome.stop_reason == replayed.stop_reason == "invalid_output_limit"
    assert outcome.final_diff == replayed.final_diff
    assert outcome.metrics == replayed.metrics.to_dict()
    assert agent.run.task.lifecycle.stop_reason == "invalid_output_limit"
    assert agent.run.metrics.model_request_count == 8


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

    outcome = agent.ask("Create subject.txt", **VERIFIED_MODIFY_TASK)

    assert outcome.answer == "Stopped after repeated rejected completion attempts."
    assert agent.run.task.lifecycle.stop_reason == "completion_block_limit"
    events = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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

    assert agent.ask("Inspect hello", **READ_TASK).answer == "Done."
    assert len(agent.model_client.prompts) == 2
    assert agent.model_client.prompts[0] == agent.model_client.prompts[1]
    assert agent.model_client.recorded_action_results[0][0] == "tool"
    result = json.loads(agent.model_client.recorded_action_results[0][1])
    assert result["status"] == "success"
    assert result["correction_action"] == "continue"
    assert result["structured"]["path"] == "hello.txt"
    assert "alpha" in result["content"]
    turns = [
        entry for entry in agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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

    assert agent.ask("Inspect hello", **READ_TASK).answer == "Done after reset."
    assert client.prompts[0] != client.prompts[1]
    assert client.prompts[1] == client.prompts[2]
    assert "alpha" in client.prompts[1]
    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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

    assert agent.ask("Inspect hello", **READ_TASK).answer == "Done without reset."
    assert client.prompts[0] == client.prompts[1]
    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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

    assert agent.ask("Inspect hello", **READ_TASK).answer == "Done after guided reset."
    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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
                raise ProviderContextOverflow("redacted context overflow")
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

    assert (
        agent.ask("Read both files and finish", **READ_TASK).answer
        == "Recovered after compaction."
    )
    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    assert sum(entry.kind == "compaction" for entry in entries) == 0
    resets = [entry for entry in entries if entry.kind == "provider_session_reset"]
    assert [entry.payload["reason"] for entry in resets] == [
        "context_overflow_retry"
    ]
    assert client.request_count == 4


def test_second_consecutive_typed_context_overflow_is_not_retried(tmp_path):
    class OverflowClient(FakeModelClient):
        request_count = 0

        def complete_action(self, *args, **kwargs):
            self.request_count += 1
            raise ProviderContextOverflow("redacted context overflow")

    client = OverflowClient([])
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto"),
    )

    with pytest.raises(ProviderContextOverflow):
        agent.ask("Inspect the workspace", **READ_TASK)

    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    resets = [entry for entry in entries if entry.kind == "provider_session_reset"]
    assert [entry.payload["reason"] for entry in resets] == [
        "context_overflow_retry"
    ]
    assert client.request_count == 2


@pytest.mark.parametrize(
    "message",
    [
        "maximum context length exceeded",
        "too many tokens per minute",
    ],
)
def test_untyped_runtime_error_is_not_recovered(tmp_path, message):
    class OrdinaryFailureClient(FakeModelClient):
        request_count = 0

        def complete_action(self, *args, **kwargs):
            self.request_count += 1
            raise RuntimeError(message)

    client = OrdinaryFailureClient([])
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto"),
    )

    with pytest.raises(RuntimeError, match=message):
        agent.ask("Inspect the workspace", **READ_TASK)

    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    assert not any(entry.kind == "provider_session_reset" for entry in entries)
    assert client.request_count == 1


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

    outcome = agent.ask("Inspect hello.txt", **READ_TASK)

    assert outcome.answer == "Done at the tool boundary."
    assert agent.run.metrics.executed_tool_count == 1
    assert agent.run.task.lifecycle.status == "completed"
    assert agent.model_client.action_tool_surfaces[-1] == ("submit_final",)
    turns = [
        event for event in agent.run.run_log.events if event.kind == "turn_metrics"
    ]
    assert turns[-1].payload["prompt_reused"] is False
    assert turns[-1].payload["prompt_metadata"]["wire_tool_names"] == [
        "submit_final"
    ]
    resets = [
        event
        for event in agent.run.run_log.events
        if event.kind == "provider_session_reset"
    ]
    assert [event.payload["reason"] for event in resets] == [
        "tool_surface_changed"
    ]


def test_allowed_tools_provider_keeps_wire_schema_stable_for_read_only_run(
    tmp_path,
):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    class SchemaCountingClient(FakeModelClient):
        @staticmethod
        def estimate_action_tool_tokens(action_tools, _token_counter):
            return len(action_tools)

    client = SchemaCountingClient(
        [
            ModelAction.tool(
                "read_file",
                {"path": "hello.txt", "start_line": 1, "end_line": 1},
            ),
            ModelAction.final("Done."),
        ]
    )
    client.supports_allowed_tools = True
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto"),
    )

    assert agent.ask("Inspect hello.txt", **READ_TASK).answer == "Done."

    declared_names = tuple(tool["name"] for tool in agent.tools.action_schemas)
    assert client.action_tool_surfaces == [declared_names, declared_names]
    assert client.allowed_tool_name_surfaces[0] == (
        client.allowed_tool_name_surfaces[1]
    )
    assert "read_file" in client.allowed_tool_name_surfaces[0]
    assert "submit_final" in client.allowed_tool_name_surfaces[0]
    assert "write_file" not in client.allowed_tool_name_surfaces[0]
    assert "memory_store" not in client.allowed_tool_name_surfaces[0]
    turns = [
        event
        for event in agent.run.run_log.events
        if event.kind == "turn_metrics"
    ]
    assert all(
        event.payload["prompt_metadata"]["tool_schema_tokens"]
        == len(declared_names)
        for event in turns
    )
    assert all(
        tuple(event.payload["prompt_metadata"]["wire_tool_names"])
        == declared_names
        for event in turns
    )


def test_allowed_tools_provider_uses_physical_final_only_wire_schema(
    tmp_path,
):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    client = FakeModelClient(
        [
            ModelAction.tool(
                "read_file",
                {"path": "hello.txt", "start_line": 1, "end_line": 1},
            ),
            ModelAction.final("Done at the boundary."),
        ]
    )
    client.supports_allowed_tools = True
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            max_tool_executions=1,
        ),
    )

    assert agent.ask("Inspect hello.txt", **READ_TASK).answer == (
        "Done at the boundary."
    )

    declared_names = tuple(tool["name"] for tool in agent.tools.action_schemas)
    assert client.action_tool_surfaces == [declared_names, ("submit_final",)]
    assert client.allowed_tool_name_surfaces[-1] == ("submit_final",)
    resets = [
        event
        for event in agent.run.run_log.events
        if event.kind == "provider_session_reset"
    ]
    assert [event.payload["reason"] for event in resets] == [
        "tool_surface_changed"
    ]


def test_unsupported_provider_uses_run_fixed_read_only_wire_surface(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    class SchemaCountingClient(FakeModelClient):
        @staticmethod
        def estimate_action_tool_tokens(action_tools, _token_counter):
            return len(action_tools)

    client = SchemaCountingClient(
        [
            ModelAction.tool(
                "read_file",
                {"path": "hello.txt", "start_line": 1, "end_line": 1},
            ),
            ModelAction.final("Done."),
        ]
    )
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto"),
    )

    assert agent.ask("Inspect hello.txt", **READ_TASK).answer == "Done."

    assert client.action_tool_surfaces[0] == client.action_tool_surfaces[1]
    assert client.action_tool_surfaces == client.allowed_tool_name_surfaces
    assert "read_file" in client.action_tool_surfaces[0]
    assert "write_file" not in client.action_tool_surfaces[0]
    assert "memory_store" not in client.action_tool_surfaces[0]
    turns = [
        event
        for event in agent.run.run_log.events
        if event.kind == "turn_metrics"
    ]
    assert all(
        event.payload["prompt_metadata"]["tool_schema_tokens"]
        == len(client.action_tool_surfaces[0])
        for event in turns
    )


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

    outcome = agent.ask("Read seven distinct lines", **READ_TASK)

    assert outcome.answer == "Completed seven reads."
    assert agent.config.max_tool_executions is None
    assert agent.run.metrics.executed_tool_count == 7


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

    assert (
        agent.ask("Inspect hello.txt", **READ_TASK).answer
        == "First run completed."
    )
    assert (
        agent.ask("Summarize the prior run", **NO_CHANGE_TASK).answer
        == "Second run completed."
    )

    second_run_prompt = agent.model_client.prompts[2]
    assert "Inspect hello.txt" not in second_run_prompt
    assert "First run completed." not in second_run_prompt
    assert "unique-tool-output" not in second_run_prompt
    assert "Current run events" not in second_run_prompt
    assert second_run_prompt.endswith(
        'task_request:\n"Summarize the prior run"'
    )
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

    outcome = agent.ask("Inspect hello.txt", **READ_TASK)

    assert outcome.answer == (
        "Stopped after reaching the tool execution limit without a final answer."
    )
    assert agent.run.metrics.executed_tool_count == 1
    finished_tools = [
        entry.name
        for entry in agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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

    outcome = agent.ask("Inspect hello.txt", **READ_TASK)

    assert outcome.answer == "Recovered after correcting the call."
    assert agent.run.metrics.executed_tool_count == 1
    assert agent.run.metrics.model_request_count == 3
    results = [
        entry
        for entry in agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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
    assert agent.ask("What is the deploy target?", **READ_TASK).answer == "staging"
    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
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
    assert agent.run.metrics.model_request_count == 2


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
    assert agent.ask("Inspect memory", **NO_CHANGE_TASK).answer == "Done."
    result = next(
        entry
        for entry in agent.run.run_log.events
        if entry.kind == "tool_result" and entry.name == "memory_recall"
    )
    assert result.outcome_status == "rejected"
    assert "unavailable filename" in result.content
