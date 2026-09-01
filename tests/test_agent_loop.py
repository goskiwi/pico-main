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
from pico.command_runner import CommandResult
from pico.contracts import ToolOutcome
from pico.evidence import verification_is_current
from pico.mutations import content_revision, file_revision
from pico.providers import ProviderContextOverflow

READ_TASK = {
    "intent": "read_only",
}
NO_CHANGE_TASK = {
    "intent": "modify_optional",
}
MODIFY_TASK = {
    "intent": "modify",
}
VERIFIED_MODIFY_TASK = {
    "intent": "modify",
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

    outcome = AgentLoop(agent).run(
        "Inspect hello.txt", task_intent="read_only"
    )

    assert outcome.answer == "Done."
    assert agent.run.task.lifecycle.status == "completed"

    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    assert [entry.sequence for entry in entries] == list(range(1, len(entries) + 1))
    tool_entry = next(entry for entry in entries if entry.kind == "tool_result")
    outcome = tool_entry.payload["outcome"]
    assert outcome["artifact_id"] == ""


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

    class RecordingVerificationCommandRunner:
        @staticmethod
        def run(*_args, **_kwargs):
            verified_contents.append(target.read_text(encoding="utf-8"))
            return CommandResult(returncode=0, stdout="1 passed\n")

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
        command_runner=RecordingVerificationCommandRunner(),
    )

    outcome = agent._ask_with_intent(
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
        "recommended_tool_args": {
            "path": "subject.txt",
            "start_line": 1,
            "end_line": 200,
        },
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

    outcome = agent._ask_with_intent("Inspect the repository", **READ_TASK)

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
    class FailingCommandRunner:
        @staticmethod
        def run(*_args, **_kwargs):
            return CommandResult(returncode=1, stderr="assertion failed")

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
        command_runner=FailingCommandRunner(),
    )

    outcome = agent._ask_with_intent("Create subject.txt", **VERIFIED_MODIFY_TASK)

    assert outcome.answer == "Stopped after repeated rejected completion attempts."
    assert agent.run.task.lifecycle.stop_reason == "completion_block_limit"
    events = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    assert sum(entry.kind == "completion_blocked" for entry in events) == 3


def test_verifier_created_file_prevents_successful_completion(tmp_path):
    class MutatingVerificationRunner:
        @staticmethod
        def run(*_args, **_kwargs):
            (tmp_path / "verifier-extra.txt").write_text(
                "unexpected\n",
                encoding="utf-8",
            )
            return CommandResult(returncode=0, stdout="tests passed")

    agent = Pico(
        FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {"path": "subject.txt", "content": "changed\n"},
                ),
                ModelAction.final("done"),
            ]
        ),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="verify",
        ),
        command_runner=MutatingVerificationRunner(),
    )

    with pytest.raises(RuntimeError, match="fake model ran out of outputs"):
        agent._ask_with_intent("Create subject.txt", **VERIFIED_MODIFY_TASK)

    events = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    verification = next(
        event for event in events if event.kind == "verification_result"
    )
    assert verification.payload["status"] == "failed"
    assert "verifier-extra.txt" in verification.payload["output"]
    assert any(event.kind == "completion_blocked" for event in events)
    assert not any(event.kind == "assistant_final" for event in events)
    assert agent.run.projection.terminal is False
    assert agent.run.projection.final_diff is None
    assert (tmp_path / "verifier-extra.txt").is_file()


def test_tool_turn_reuses_initial_prompt_and_records_provider_result(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "hello.txt", "start_line": 1, "end_line": 1}),
            ModelAction.final("Done."),
        ],
    )

    assert agent._ask_with_intent("Inspect hello", **READ_TASK).answer == "Done."
    assert len(agent.model_client.prompts) == 2
    assert agent.model_client.prompts[0] == agent.model_client.prompts[1]
    result = json.loads(agent.model_client.recorded_action_results[0])
    assert result["status"] == "success"
    assert result["correction_action"] == "continue"
    assert result["structured"]["path"] == "hello.txt"
    assert "alpha" in result["content"]
    turns = [
        entry for entry in agent.dependencies.run_store.read_events(agent.run.projection.run_id)
        if entry.kind == "turn_metrics"
    ]
    assert [entry.payload for entry in turns] == [
        {"input_tokens": None, "output_tokens": None},
        {"input_tokens": None, "output_tokens": None},
    ]
    assert not any(
        entry.kind == "provider_session_reset"
        for entry in agent.dependencies.run_store.read_events(
            agent.run.projection.run_id
        )
    )


def test_provider_session_resets_at_actual_input_high_watermark(tmp_path):
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
    original_count = agent.prompt._context.tokenizer.count
    agent.prompt._context.tokenizer.count = lambda text: (
        200 if "alpha" in str(text) else original_count(text)
    )

    assert agent._ask_with_intent("Inspect hello", **READ_TASK).answer == "Done after reset."
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
        "reason": "context_high_watermark",
        "input_tokens": 6500,
        "threshold_tokens": 6000,
    }
    turns = [
        entry for entry in entries if entry.kind == "turn_metrics"
    ]
    assert [entry.payload["input_tokens"] for entry in turns] == [
        6500,
        1000,
        1000,
    ]


def test_provider_session_continues_below_actual_input_high_watermark(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")

    class CapacityClient(FakeModelClient):
        def complete_action(self, *args, **kwargs):
            action = super().complete_action(*args, **kwargs)
            self.last_completion_metadata = {
                "input_tokens": 5999,
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
    original_count = agent.prompt._context.tokenizer.count
    agent.prompt._context.tokenizer.count = lambda text: (
        200 if "alpha" in str(text) else original_count(text)
    )

    assert agent._ask_with_intent("Inspect hello", **READ_TASK).answer == "Done without reset."
    assert client.prompts[0] == client.prompts[1]
    entries = agent.dependencies.run_store.read_events(agent.run.projection.run_id)
    assert not any(
        entry.kind == "provider_session_reset" for entry in entries
    )


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
        agent._ask_with_intent("Read both files and finish", **READ_TASK).answer
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
        agent._ask_with_intent("Inspect the workspace", **READ_TASK)

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
        agent._ask_with_intent("Inspect the workspace", **READ_TASK)

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

    outcome = agent._ask_with_intent("Inspect hello.txt", **READ_TASK)

    assert outcome.answer == "Done at the tool boundary."
    assert agent.run.metrics.executed_tool_count == 1
    assert agent.run.task.lifecycle.status == "completed"
    assert agent.model_client.action_tool_surfaces[-1] == ("submit_final",)
    resets = [
        event
        for event in agent.run.run_log.events
        if event.kind == "provider_session_reset"
    ]
    assert [event.payload["reason"] for event in resets] == [
        "tool_surface_changed"
    ]


def test_read_only_run_sends_only_current_tool_surface(tmp_path):
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

    assert agent._ask_with_intent("Inspect hello.txt", **READ_TASK).answer == "Done."

    names = client.action_tool_surfaces[0]
    assert client.action_tool_surfaces == [names, names]
    assert "read_file" in names
    assert "run_command" in names
    assert "submit_final" in names
    assert "write_file" not in names


def test_tool_budget_switches_to_final_only_surface(tmp_path):
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
    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            max_tool_executions=1,
        ),
    )

    assert agent._ask_with_intent("Inspect hello.txt", **READ_TASK).answer == (
        "Done at the boundary."
    )

    assert "read_file" in client.action_tool_surfaces[0]
    assert "write_file" not in client.action_tool_surfaces[0]
    assert client.action_tool_surfaces[-1] == ("submit_final",)
    resets = [
        event
        for event in agent.run.run_log.events
        if event.kind == "provider_session_reset"
    ]
    assert [event.payload["reason"] for event in resets] == [
        "tool_surface_changed"
    ]

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
        agent._ask_with_intent("Inspect hello.txt", **READ_TASK).answer
        == "First run completed."
    )
    assert (
        agent._ask_with_intent("Summarize the prior run", **NO_CHANGE_TASK).answer
        == "Second run completed."
    )

    second_run_prompt = agent.model_client.prompts[2]
    assert "Inspect hello.txt" not in second_run_prompt
    assert "First run completed." not in second_run_prompt
    assert "unique-tool-output" not in second_run_prompt
    assert "Current run events" not in second_run_prompt
    assert second_run_prompt.index(
        'task_request:\n"Summarize the prior run"'
    ) < second_run_prompt.index('<untrusted_context trust="untrusted_data">')
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

    outcome = agent._ask_with_intent("Inspect hello.txt", **READ_TASK)

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

    outcome = agent._ask_with_intent("Inspect hello.txt", **READ_TASK)

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
