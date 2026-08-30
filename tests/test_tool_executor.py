import os
import time
from unittest.mock import patch

import pytest

import pico.mutations as mutation_module
from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.completion_controller import CompletionController
from pico.contracts import FailureInfo, ToolCall, ToolOutcome, ToolRunnerResult
from pico.delivery import build_final_diff_descriptor
from pico.execution import ExecutionContext
from pico.mutations import file_revision
from pico.run_lifecycle import AgentLoopState, RunLifecycle
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto"),
    )


def start_run(agent, *, run_id="run_tool_test", goal="Exercise tools"):
    run_log = RunLog(
        run_id,
        "task_tool_test",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    first = run_log.append_user(
        TaskContract(
            goal=goal,
            task_kind="modify",
            requires_workspace_change=False,
            requires_verification=False,
        )
    )
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.run_log = run_log
    return run_log


def run_active(agent, call):
    run_log = agent.run.run_log or start_run(agent)
    agent.apply_run_event(run_log.append_tool_call(call))
    return agent.tools.run(call)


def test_tool_executor_returns_canonical_outcome_and_artifact(tmp_path):
    agent = build_agent(tmp_path)

    outcome = agent.tools.run(
        ToolCall("read_file", {"path": "README.md", "start_line": 1, "end_line": 1}, "call_test")
    )

    assert isinstance(outcome, ToolOutcome)
    assert outcome.status == "success"
    assert outcome.execution_state == "completed"
    assert outcome.side_effect_state == "none"
    assert set(outcome.to_dict()) == {
        "tool_call_id",
        "tool_name",
        "status",
        "execution_state",
        "side_effect_state",
        "content",
        "structured",
        "failure",
        "affected_paths",
        "effect_scope",
        "artifact",
    }
    assert outcome.artifact == {}
    assert not (tmp_path / ".pico" / "runs" / "manual" / "artifacts").exists()


def test_rejected_call_never_enters_execution(tmp_path):
    outcome = build_agent(tmp_path).tools.run(
        ToolCall("missing", {}, "call_missing")
    )

    assert outcome.status == "rejected"
    assert outcome.execution_state == "not_started"
    assert outcome.failure.code == "unknown_tool"


def test_artifact_rejects_old_schema_and_detects_tampering(tmp_path):
    agent = build_agent(tmp_path)
    agent.tools.registry["list_files"]["run"] = lambda _args: ToolRunnerResult(
        "large output\n" * 2000
    )
    outcome = agent.tools.run(ToolCall("list_files", {}, "call_artifact"))
    root = tmp_path / ".pico" / "runs" / "manual" / "artifacts"
    descriptor_path = root / f"{outcome.artifact_id}.json"
    descriptor = descriptor_path.read_text(encoding="utf-8")
    descriptor_path.write_text(
        descriptor.replace("artifact-v3", "artifact-v2"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unsupported artifact schema"):
        agent.dependencies.artifacts.read_slice(
            "manual", outcome.artifact_id, 0, 8192
        )

    descriptor_path.write_text(descriptor, encoding="utf-8")
    (root / f"{outcome.artifact_id}.txt").write_text(
        "tampered", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        agent.dependencies.artifacts.read_slice(
            "manual", outcome.artifact_id, 0, 8192
        )


def test_tool_outputs_and_failures_are_redacted_before_leaving_executor(tmp_path):
    secret = "tool-output-secret-value-987654"
    (tmp_path / "secret.txt").write_text(secret + "\n", encoding="utf-8")
    client = FakeModelClient(
        [
            ModelAction.tool(
                "read_file",
                {"path": "secret.txt", "start_line": 1, "end_line": 1},
            ),
            ModelAction.final("Done."),
        ]
    )

    with patch.dict(os.environ, {"CUSTOM_SECRET_NAME": secret}):
        agent = Pico(
            client,
            WorkspaceContext.build(tmp_path),
            SessionStore(tmp_path / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="auto",
                verification_command="",
                secret_env_names=frozenset({"CUSTOM_SECRET_NAME"}),
            ),
        )
        assert agent.ask(
            "Read secret.txt",
            task_kind="read_only",
            requires_workspace_change=False,
            requires_verification=False,
        ) == "Done."
        provider_result = client.recorded_action_results[0][1]
        event = next(
            item for item in agent.run.run_log.events if item.kind == "tool_result"
        )
        persisted = event.payload["outcome"]["content"]
        agent.reset()

        def fail_with_secret(_args):
            raise RuntimeError(secret)

        agent.tools.registry["list_files"]["run"] = fail_with_secret
        failed = agent.tools.run(ToolCall("list_files", {}, "call_secret_failure"))

        (tmp_path / "state-change.txt").write_text("changed\n", encoding="utf-8")
        agent.tools.registry["list_files"]["run"] = lambda _args: ToolRunnerResult(
            (secret + "\n") * 2000,
            structured={"secret": secret},
        )
        large = agent.tools.run(ToolCall("list_files", {}, "call_secret_artifact"))
        artifact_path = (
            tmp_path
            / ".pico"
            / "runs"
            / "manual"
            / "artifacts"
            / f"{large.artifact_id}.txt"
        )
        artifact_content = artifact_path.read_text(encoding="utf-8")

    assert secret not in provider_result
    assert secret not in persisted
    assert secret not in failed.content
    assert secret not in failed.failure.detail
    assert secret not in large.structured["secret"]
    assert secret not in artifact_content
    assert "<redacted>" in provider_result
    assert "<redacted>" in persisted
    assert "<redacted>" in artifact_content
    assert large.structured["secret"] == "<redacted>"


def test_large_tool_output_keeps_full_artifact_and_bounded_outcome(tmp_path):
    target = tmp_path / "large.txt"
    target.write_text(
        "prefix\n" + "x" * 9000 + "middle-only-marker" + "x" * 9000
        + "\nfull-artifact-tail\n"
    )
    agent = build_agent(tmp_path)

    outcome = agent.tools.run(
        ToolCall("read_file", {"path": "large.txt", "start_line": 1, "end_line": 20}, "call_large")
    )

    artifact = (
        tmp_path / ".pico" / "runs" / "manual" / "artifacts" / f"{outcome.artifact_id}.txt"
    )
    assert len(outcome.content.encode("utf-8")) <= 13 * 1024
    assert len(outcome.content) < len(artifact.read_text(encoding="utf-8"))
    assert "middle-only-marker" not in outcome.content
    assert "full-artifact-tail" not in outcome.content
    assert "read_artifact" in outcome.content
    assert outcome.artifact_id in outcome.content
    assert "full-artifact-tail" in artifact.read_text(encoding="utf-8")
    page = agent.dependencies.artifacts.read_slice(
        "manual", outcome.artifact_id, 0, 8192
    )
    assert page["descriptor"]["size_bytes"] > 16000


def test_large_shell_output_keeps_tail_and_points_to_artifact(tmp_path):
    agent = build_agent(tmp_path)
    output = "head-marker\n" + "noise\n" * 5000 + "tail-marker\n"
    agent.tools.registry["run_shell"]["run"] = (
        lambda _args: ToolRunnerResult("exit_code: 0\n" + output)
    )

    outcome = agent.tools.run(
        ToolCall("run_shell", {"command": "true", "timeout_seconds": 20}, "call_shell_large")
    )

    assert "head-marker" not in outcome.content
    assert "tail-marker" in outcome.content
    assert "read_artifact" in outcome.content
    artifact = (
        tmp_path / ".pico" / "runs" / "manual" / "artifacts"
        / f"{outcome.artifact_id}.txt"
    )
    assert "head-marker" in artifact.read_text(encoding="utf-8")


def test_runner_failure_is_structured_and_content_is_not_parsed(tmp_path):
    failed_agent = build_agent(tmp_path)
    failed_agent.tools.registry["run_shell"]["run"] = lambda _args: ToolRunnerResult(
        "command display format changed",
        failure=FailureInfo(
            "command_failed",
            "command exited with 7",
            "retry_after_change",
        ),
    )

    failed = failed_agent.tools.run(
        ToolCall("run_shell", {"command": "false", "timeout_seconds": 20}, "call_failed")
    )

    assert failed.status == "error"
    assert failed.execution_state == "completed"
    assert failed.failure.code == "command_failed"

    successful_agent = build_agent(tmp_path)
    successful_agent.tools.registry["run_shell"]["run"] = (
        lambda _args: ToolRunnerResult("exit_code: 99")
    )

    successful = successful_agent.tools.run(
        ToolCall("run_shell", {"command": "true", "timeout_seconds": 20}, "call_success")
    )

    assert successful.status == "success"
    assert successful.failure is None


def test_tool_outcome_rejects_impossible_execution_states():
    with pytest.raises(ValueError, match="successful outcome must complete"):
        ToolOutcome(
            tool_call_id="call_invalid",
            tool_name="read_file",
            status="success",
            execution_state="not_started",
            side_effect_state="none",
            content="invalid",
        )


def test_failure_info_rejects_old_retryable_field():
    with pytest.raises(ValueError, match="invalid failure information"):
        FailureInfo.from_dict(
            {
                "code": "command_failed",
                "detail": "failed",
                "retryable": True,
            }
        )


def test_tool_outcome_requires_consistent_effect_facts():
    failure = FailureInfo("tool_partial_success", "partial", "no_retry")

    with pytest.raises(ValueError, match="known side effects require affected paths"):
        ToolOutcome(
            tool_call_id="call_partial",
            tool_name="write_file",
            status="partial_success",
            execution_state="failed",
            side_effect_state="partial",
            content="partial",
            failure=failure,
            effect_scope="workspace",
        )

    with pytest.raises(ValueError, match="unknown side effects require an effect scope"):
        ToolOutcome(
            tool_call_id="call_unknown",
            tool_name="write_file",
            status="partial_success",
            execution_state="failed",
            side_effect_state="unknown",
            content="unknown",
            failure=failure,
        )


def test_tool_runner_rejects_legacy_string_result(tmp_path):
    agent = build_agent(tmp_path)
    agent.tools.registry["list_files"]["run"] = lambda _args: "legacy result"

    outcome = agent.tools.run(ToolCall("list_files", {}, "call_legacy_runner"))

    assert outcome.status == "error"
    assert outcome.failure.detail == "tool runner must return ToolRunnerResult"


def test_manual_mutation_is_rejected_without_touching_workspace(tmp_path):
    agent = build_agent(tmp_path)

    outcome = agent.tools.run(
        ToolCall(
            "write_file",
            {"path": "manual.txt", "content": "must not exist\n"},
            "call_manual_write",
        )
    )

    assert outcome.status == "rejected"
    assert outcome.failure.code == "manual_mutation_forbidden"
    assert not (tmp_path / "manual.txt").exists()


def test_runtime_mutation_records_diff_transition_and_final_diff(tmp_path):
    agent = build_agent(tmp_path)
    outcome = run_active(
        agent,
        ToolCall(
            "write_file",
            {"path": "created.txt", "content": "alpha\n"},
            "call_create",
        ),
    )

    transition = outcome.structured["path_transitions"][0]
    assert outcome.status == "success"
    assert "--- /dev/null" in outcome.content
    assert "+alpha" in outcome.content
    assert transition == {
        "path": "created.txt",
        "before_state": "absent",
        "after_state": outcome.structured["after_revision"],
        "before_artifact_id": "",
    }
    final_diff = build_final_diff_descriptor(agent)
    assert final_diff.diff_artifact_id.startswith("diff_")
    assert final_diff.diff_bytes > 0
    assert "+alpha" in agent.dependencies.artifacts.read_internal_text(
        "run_tool_test", final_diff.diff_artifact_id
    )
    agent.apply_run_event(
        agent.run.run_log.append_final("created", final_diff)
    )
    replayed = agent.dependencies.run_store.replay("run_tool_test")
    assert replayed.final_diff == final_diff


def test_terminal_replay_rejects_missing_final_diff_artifact(tmp_path):
    agent = build_agent(tmp_path)
    run_active(
        agent,
        ToolCall(
            "write_file",
            {"path": "created.txt", "content": "alpha\n"},
            "call_missing_final_diff",
        ),
    )
    final_diff = build_final_diff_descriptor(agent)
    agent.apply_run_event(agent.run.run_log.append_final("created", final_diff))
    artifact_root = agent.dependencies.run_store.artifact_dir("run_tool_test")
    (artifact_root / f"{final_diff.diff_artifact_id}.txt").unlink()

    with pytest.raises(ValueError, match="internal artifact is missing"):
        agent.dependencies.run_store.replay("run_tool_test")


def test_existing_file_preimage_is_saved_only_on_first_runtime_mutation(tmp_path):
    target = tmp_path / "subject.txt"
    target.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path)

    first = run_active(
        agent,
        ToolCall(
            "edit_file",
            {
                "path": "subject.txt",
                "old_text": "alpha",
                "new_text": "beta",
                "expected_revision": file_revision(target),
            },
            "call_edit_first",
        ),
    )
    second = run_active(
        agent,
        ToolCall(
            "edit_file",
            {
                "path": "subject.txt",
                "old_text": "beta",
                "new_text": "gamma",
                "expected_revision": file_revision(target),
            },
            "call_edit_second",
        ),
    )

    first_artifact = first.structured["path_transitions"][0]["before_artifact_id"]
    second_artifact = second.structured["path_transitions"][0]["before_artifact_id"]
    assert first_artifact.startswith("preimage_")
    assert second_artifact == ""
    assert agent.dependencies.artifacts.read_internal_text(
        "run_tool_test", first_artifact
    ) == "alpha\n"


def test_memory_write_is_audited_as_control_effect_with_runtime_provenance(tmp_path):
    agent = build_agent(tmp_path)
    run_log = start_run(agent, run_id="run_memory", goal="Remember")
    call = ToolCall(
        "memory_store",
        {
            "action": "create",
            "filename": "project_release_command.md",
            "name": "Release command",
            "description": "Stable command used before release.",
            "memory_type": "project",
            "content": "Run `python -m pytest -q`.",
            "why": "The user explicitly requested this convention.",
            "how_to_apply": "Run it before release.",
            "expires_at": "",
        },
        "call_memory",
    )
    source = agent.apply_run_event(run_log.append_tool_call(call))

    outcome = agent.tools.run(call)

    card = agent.dependencies.project_memory.recall("project_release_command.md")
    assert outcome.status == "success"
    assert outcome.side_effect_state == "changed"
    assert outcome.effect_scope == "project_memory"
    assert ".pico/memory/cards/project_release_command.md" in outcome.affected_paths
    assert agent.run.evidence.changed_paths == []
    assert (
        ".pico/memory/cards/project_release_command.md"
        in agent.run.evidence.effects[-1]["affected_paths"]
    )
    assert agent.run.evidence.effects[-1]["effect_scope"] == "project_memory"
    assert source.call_id == call.call_id
    assert card.source_run_id == "run_memory"
    assert card.source_tool_call_id == call.call_id


def test_successful_identical_observation_is_allowed(tmp_path):
    agent = build_agent(tmp_path)
    args = {"path": "README.md", "start_line": 1, "end_line": 1}

    first = agent.tools.run(ToolCall("read_file", args, "call_read_1"))
    repeated = agent.tools.run(ToolCall("read_file", args, "call_read_2"))
    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")
    after_change = agent.tools.run(ToolCall("read_file", args, "call_read_3"))

    assert first.status == "success"
    assert repeated.status == "success"
    assert after_change.status == "success"
    assert "changed" in after_change.content


def test_repeated_read_remains_allowed_across_workspace_change(tmp_path):
    agent = build_agent(tmp_path)
    read_args = {"path": "README.md", "start_line": 1, "end_line": 1}

    first = agent.tools.run(ToolCall("read_file", read_args, "call_read_before"))
    changed = run_active(
        agent,
        ToolCall(
            "write_file",
            {
                "path": "unrelated.txt",
                "content": "new file\n",
            },
            "call_write_unrelated",
        )
    )
    repeated = run_active(
        agent, ToolCall("read_file", read_args, "call_read_after")
    )

    assert first.status == "success"
    assert changed.status == "success"
    assert repeated.status == "success"


def test_workspace_wide_observation_can_repeat_without_workspace_change(tmp_path):
    agent = build_agent(tmp_path)
    executions = []
    agent.tools.registry["run_shell"]["run"] = lambda _args: (
        executions.append(1) or ToolRunnerResult("exit_code: 0")
    )
    shell_args = {"command": "true", "timeout_seconds": 20}

    first = agent.tools.run(ToolCall("run_shell", shell_args, "call_shell_before"))
    second = agent.tools.run(ToolCall("run_shell", shell_args, "call_shell_after"))

    assert first.status == "success"
    assert second.status == "success"
    assert len(executions) == 2


def test_retry_after_wait_error_does_not_block_identical_retries(tmp_path):
    agent = build_agent(tmp_path)
    executions = []

    def fail(_args):
        executions.append(1)
        return ToolRunnerResult(
            "command did not report an exit code",
            failure=FailureInfo(
                "command_result_missing",
                "command did not report an exit code",
                "retry_after_wait",
            ),
        )

    agent.tools.registry["run_shell"]["run"] = fail
    args = {"command": "true", "timeout_seconds": 20}

    first = agent.tools.run(ToolCall("run_shell", args, "call_shell_1"))
    second = agent.tools.run(ToolCall("run_shell", args, "call_shell_2"))
    third = agent.tools.run(ToolCall("run_shell", args, "call_shell_3"))

    assert first.status == "error"
    assert first.correction_action == "wait"
    assert second.status == "error"
    assert second.correction_action == "wait"
    assert third.status == "error"
    assert third.correction_action == "wait"
    assert len(executions) == 3


def test_retry_after_change_error_does_not_block_identical_retry(tmp_path):
    agent = build_agent(tmp_path)
    executions = []

    def fail(_args):
        executions.append(1)
        raise RuntimeError("deterministic executor failure")

    agent.tools.registry["run_shell"]["run"] = fail
    args = {"command": "true", "timeout_seconds": 20}

    first = agent.tools.run(ToolCall("run_shell", args, "call_change_1"))
    second = agent.tools.run(ToolCall("run_shell", args, "call_change_2"))

    assert first.status == "error"
    assert first.failure.recovery == "retry_after_change"
    assert first.correction_action == "repair"
    assert second.status == "error"
    assert second.correction_action == "repair"
    assert len(executions) == 2


def test_workspace_mutation_forces_prompt_refresh(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    refresh_calls = []

    def changed_refresh(*, force=False):
        refresh_calls.append(force)
        return force

    monkeypatch.setattr(agent.workspace, "refresh", changed_refresh)

    outcome = run_active(
        agent,
        ToolCall(
            "write_file",
            {
                "path": "created.txt",
                "content": "created\n",
            },
            "write_refresh_change",
        )
    )

    assert outcome.status == "success"
    assert True in refresh_calls


def test_partial_side_effect_blocks_blind_identical_replay(tmp_path):
    agent = build_agent(tmp_path)
    executions = []

    def write_then_fail(args):
        executions.append(1)
        target = tmp_path / args["path"]
        target.write_text(args["content"], encoding="utf-8")
        return ToolRunnerResult(
            "failed after write",
            structured={
                "path_transitions": [
                    {
                        "path": args["path"],
                        "before_state": "absent",
                        "after_state": file_revision(target),
                    }
                ]
            },
            affected_paths=(args["path"],),
            effect_scope="workspace",
            failure=FailureInfo("partial_write", "failed after write", "no_retry"),
        )

    agent.tools.registry["write_file"]["run"] = write_then_fail
    args = {
        "path": "partial.txt",
        "content": "partial\n",
    }

    first = run_active(agent, ToolCall("write_file", args, "call_write_1"))
    repeated = run_active(agent, ToolCall("write_file", args, "call_write_2"))

    assert first.status == "partial_success"
    assert first.side_effect_state == "partial"
    assert repeated.status == "rejected"
    assert "inspect state" in repeated.failure.detail
    assert len(executions) == 1


def test_unknown_runner_exception_after_write_records_replayable_transition(tmp_path):
    agent = build_agent(tmp_path)

    def write_then_raise(args):
        (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
        raise RuntimeError("crashed after write")

    agent.tools.registry["write_file"]["run"] = write_then_raise
    outcome = run_active(
        agent,
        ToolCall(
            "write_file",
            {"path": "partial.txt", "content": "partial\n"},
            "call_partial_exception",
        ),
    )

    assert outcome.status == "partial_success"
    transition = outcome.structured["path_transitions"][0]
    assert transition["before_state"] == "absent"
    assert transition["after_state"] == file_revision(tmp_path / "partial.txt")
    replayed = agent.dependencies.run_store.replay("run_tool_test")
    assert replayed.evidence.touched_paths == ["partial.txt"]


def test_external_drift_is_rejected_before_a_second_runtime_mutation(tmp_path):
    target = tmp_path / "subject.txt"
    target.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path)

    first = run_active(
        agent,
        ToolCall(
            "edit_file",
            {
                "path": "subject.txt",
                "old_text": "alpha",
                "new_text": "beta",
                "expected_revision": file_revision(target),
            },
            "call_first_edit",
        ),
    )
    target.write_text("external\n", encoding="utf-8")
    second = run_active(
        agent,
        ToolCall(
            "edit_file",
            {
                "path": "subject.txt",
                "old_text": "external",
                "new_text": "agent-second",
                "expected_revision": file_revision(target),
            },
            "call_second_edit",
        ),
    )

    assert first.status == "success"
    assert second.status == "rejected"
    assert second.execution_state == "not_started"
    assert second.failure.code == "workspace_drift"
    assert second.failure.recovery == "user_action_required"
    assert second.structured["drift"][0]["path"] == "subject.txt"
    assert target.read_text(encoding="utf-8") == "external\n"
    assessment = CompletionController(agent).assess("done")
    assert assessment.allowed is False
    assert assessment.status == "workspace_drift"
    second_events = [
        event.kind
        for event in agent.run.run_log.events
        if event.call_id == "call_second_edit"
    ]
    assert second_events == ["assistant_tool_call", "tool_result"]
    replayed = agent.dependencies.run_store.replay("run_tool_test")
    assert replayed.evidence.change_set.files["subject.txt"].current_after_state == (
        first.structured["after_revision"]
    )


@pytest.mark.parametrize("stop_mode", ["cancel", "reset"])
def test_controlled_stop_survives_external_workspace_drift(tmp_path, stop_mode):
    target = tmp_path / "subject.txt"
    target.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path)
    run_active(
        agent,
        ToolCall(
            "edit_file",
            {
                "path": "subject.txt",
                "old_text": "alpha",
                "new_text": "beta",
                "expected_revision": file_revision(target),
            },
            "call_first_edit",
        ),
    )
    target.write_text("external\n", encoding="utf-8")
    rejected = run_active(
        agent,
        ToolCall(
            "edit_file",
            {
                "path": "subject.txt",
                "old_text": "external",
                "new_text": "agent-second",
                "expected_revision": file_revision(target),
            },
            "call_second_edit",
        ),
    )
    assert rejected.failure.code == "workspace_drift"
    agent.session.set_active_run("run_tool_test")

    if stop_mode == "reset":
        agent.reset()
    else:
        agent.run.execution_context = ExecutionContext.root(max_seconds=30)
        assert agent.cancel_current_run("user_cancelled") is True
        loop_state = AgentLoopState(
            "continue",
            time.monotonic(),
            execution_stop="user_cancelled",
        )
        RunLifecycle(agent).finish_stopped(loop_state)

    replayed = agent.dependencies.run_store.replay("run_tool_test")
    assert replayed.terminal
    assert replayed.final_diff.unavailable_reason == "workspace_drift"
    assert replayed.final_diff.diff_artifact_id == ""
    assert replayed.final_diff.diff_bytes == 0
    assert agent.session.data["active_run_id"] == ""
    assert agent.run.execution_context is None
    if stop_mode == "reset":
        assert agent.run.task is None
        assert replayed.stop_reason == "user_reset"
    else:
        assert agent.run.projection.summary() == replayed.summary()
        assert replayed.stop_reason == "user_cancelled"


def test_commit_point_conflict_is_typed_external_drift_not_tool_partial(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    target = tmp_path / "subject.txt"
    original_replace = mutation_module.atomic_replace_bytes

    def drift_after_staging(path, payload, **options):
        original_guard = options["commit_guard"]

        def guarded_commit():
            target.write_text("external-change\n", encoding="utf-8")
            original_guard()

        return original_replace(
            path,
            payload,
            mode=options["mode"],
            commit_guard=guarded_commit,
        )

    monkeypatch.setattr(
        "pico.mutations.atomic_replace_bytes",
        drift_after_staging,
    )

    outcome = run_active(
        agent,
        ToolCall(
            "write_file",
            {
                "path": "subject.txt",
                "content": "agent-change\n",
            },
            "call_commit_conflict",
        )
    )

    assert outcome.status == "error"
    assert outcome.execution_state == "failed"
    assert outcome.side_effect_state == "none"
    assert outcome.affected_paths == ()
    assert outcome.failure.code == "revision_conflict"
    assert outcome.structured["expected_revision"] == "absent"
    assert outcome.structured["actual_revision"] == file_revision(target)
    assert target.read_text(encoding="utf-8") == "external-change\n"
