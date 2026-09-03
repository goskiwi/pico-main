import os
import threading
import time
from unittest.mock import patch

import pytest

import pico.mutations as mutation_module
import pico.tools as tools_module
from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    Workspace,
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
        workspace=Workspace.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(mode="auto"),
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
            allows_workspace_mutation=True,
            verify_changes=False,
        )
    )
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.run_log = run_log
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    return run_log


def run_active(agent, call):
    run_log = agent.run.run_log or start_run(agent)
    agent.apply_run_event(run_log.append_tool_call(call))
    return agent.tools.execute_pending(call.call_id)


def code_command_agent(tmp_path, *, approved=True):
    agent = build_agent(tmp_path)
    agent.config = PicoConfig.build(agent.config, mode="code")
    agent.tools.approve = lambda *_args, **_kwargs: approved
    return agent


def test_active_run_executes_pending_call_by_id(tmp_path):
    agent = code_command_agent(tmp_path)
    run_log = start_run(agent)
    persisted = ToolCall(
        "write_file",
        {"path": "declared.txt", "content": "declared\n"},
        "call_persisted",
    )
    agent.apply_run_event(run_log.append_tool_call(persisted))

    outcome = agent.tools.execute_pending(persisted.call_id)

    assert outcome.status == "success"
    assert outcome.affected_paths == ("declared.txt",)
    assert (tmp_path / "declared.txt").read_text(encoding="utf-8") == "declared\n"


def test_run_command_returns_diagnostic_output_without_workspace_effect(tmp_path):
    agent = code_command_agent(tmp_path)

    outcome = run_active(
        agent,
        ToolCall(
            "run_command",
            {"command": "printf command-ok"},
            "call_command_ok",
        ),
    )

    assert outcome.status == "success"
    assert outcome.side_effect_state == "none"
    assert outcome.structured["exit_code"] == 0
    assert "command-ok" in outcome.content


def test_run_command_nonzero_exit_is_repairable_without_side_effect(tmp_path):
    agent = code_command_agent(tmp_path)

    outcome = run_active(
        agent,
        ToolCall(
            "run_command",
            {"command": "printf failed >&2; exit 3"},
            "call_command_failed",
        ),
    )

    assert outcome.status == "error"
    assert outcome.side_effect_state == "none"
    assert outcome.failure.code == "command_failed"
    assert outcome.structured["exit_code"] == 3


def test_run_command_workspace_change_is_unknown_and_blocks_completion(tmp_path):
    agent = code_command_agent(tmp_path)

    outcome = run_active(
        agent,
        ToolCall(
            "run_command",
            {"command": "printf changed > generated.txt"},
            "call_command_changed",
        ),
    )

    assert outcome.status == "partial_success"
    assert outcome.side_effect_state == "unknown"
    assert outcome.effect_scope == "workspace"
    assert outcome.failure.code == "command_modified_repository"
    assert "generated.txt" in outcome.structured["repository_changes"]
    assert CompletionController(agent).assess("done").status == "partial"


def test_run_command_is_rejected_before_execution_when_approval_is_denied(tmp_path):
    agent = code_command_agent(tmp_path, approved=False)

    outcome = run_active(
        agent,
        ToolCall(
            "run_command",
            {"command": "printf forbidden > denied.txt"},
            "call_command_denied",
        ),
    )

    assert outcome.status == "rejected"
    assert outcome.failure.code == "approval_denied"
    assert not (tmp_path / "denied.txt").exists()


def test_auto_mode_rejects_hallucinated_run_command(tmp_path):
    agent = build_agent(tmp_path)

    outcome = run_active(
        agent,
        ToolCall(
            "run_command",
            {"command": "printf forbidden > denied.txt"},
            "call_auto_command",
        ),
    )

    assert outcome.status == "rejected"
    assert outcome.failure.code == "tool_not_allowed"
    assert not (tmp_path / "denied.txt").exists()


def test_pending_execution_rejects_a_different_call_id(tmp_path):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    call = ToolCall("read_file", {"path": "README.md"}, "call_expected")
    agent.apply_run_event(run_log.append_tool_call(call))

    with pytest.raises(RuntimeError, match="call id does not match"):
        agent.tools.execute_pending("call_other")


def test_tool_runtime_returns_canonical_outcome_and_artifact(tmp_path):
    agent = build_agent(tmp_path)

    outcome = agent.tools.execute_manual(
        "read_file", {"path": "README.md", "start_line": 1, "end_line": 1}
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
        "artifact_id",
    }
    assert outcome.artifact_id == ""
    assert not (tmp_path / ".pico" / "runs" / "manual" / "artifacts").exists()


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
            Workspace.build(tmp_path),
            SessionStore(tmp_path / ".pico" / "sessions"),
            config=PicoConfig(
                mode="auto",
                verification_command="",
                secret_env_names=frozenset({"CUSTOM_SECRET_NAME"}),
            ),
        )
        assert agent.ask(
            "Read secret.txt",
        ).answer == "Done."
        provider_result = client.recorded_action_results[0]
        event = next(
            item for item in agent.run.run_log.events if item.kind == "tool_result"
        )
        persisted = event.payload["outcome"]["content"]
        agent.reset()

        def fail_with_secret(_args):
            raise RuntimeError(secret)

        agent.tools.registry["list_files"]["run"] = fail_with_secret
        failed = agent.tools.execute_manual("list_files", {})

        (tmp_path / "state-change.txt").write_text("changed\n", encoding="utf-8")
        agent.tools.registry["list_files"]["run"] = lambda _args: ToolRunnerResult(
            (secret + "\n") * 2000,
            structured={"secret": secret},
        )
        large = agent.tools.execute_manual("list_files", {})
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

    outcome = agent.tools.execute_manual(
        "read_file", {"path": "large.txt", "start_line": 1, "end_line": 20}
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


def test_manual_mutation_is_rejected_without_touching_workspace(tmp_path):
    agent = build_agent(tmp_path)

    outcome = agent.tools.execute_manual(
        "write_file",
        {"path": "manual.txt", "content": "must not exist\n"},
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
        agent.dependencies.run_store.load_run("run_tool_test")
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
        agent.run.execution_context = None
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


def test_observation_batch_runs_concurrently_but_commits_results_in_call_order(
    tmp_path,
    monkeypatch,
):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name + "\n", encoding="utf-8")
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall(
            "read_file",
            {"path": "a.txt", "start_line": 1, "end_line": 1},
            "call_a",
        ),
        ToolCall(
            "read_file",
            {"path": "b.txt", "start_line": 1, "end_line": 1},
            "call_b",
        ),
    )
    batch = agent.apply_run_event(run_log.append_tool_batch(calls))
    barrier = threading.Barrier(2)
    b_finished = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    max_active = 0
    call_ids_by_path = {}

    def parallel_read(context, args):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
            call_ids_by_path[args["path"]] = context.tool_call_id()
        barrier.wait(timeout=2)
        if args["path"] == "a.txt":
            assert b_finished.wait(timeout=2)
        else:
            b_finished.set()
        with counter_lock:
            active -= 1
        return ToolRunnerResult("worker result " + args["path"])

    monkeypatch.setitem(
        tools_module._TOOL_RUNNERS,
        "read_file",
        parallel_read,
    )

    outcomes = agent.tools.execute_pending_batch(batch.batch_id)

    assert max_active == 2
    assert [outcome.tool_call_id for outcome in outcomes] == ["call_a", "call_b"]
    assert [outcome.content for outcome in outcomes] == [
        "worker result a.txt",
        "worker result b.txt",
    ]
    assert call_ids_by_path == {"a.txt": "call_a", "b.txt": "call_b"}
    assert [
        event.call_id
        for event in run_log.events
        if event.kind == "tool_result"
    ] == ["call_a", "call_b"]


def test_cancelled_observation_batch_still_closes_every_call(tmp_path):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall("list_files", {"path": "."}, "call_a"),
        ToolCall("list_files", {"path": "."}, "call_b"),
    )
    batch = agent.apply_run_event(run_log.append_tool_batch(calls))
    agent.run.execution_context.request_stop("user_cancelled")

    outcomes = agent.tools.execute_pending_batch(batch.batch_id)

    assert len(outcomes) == 2
    assert all(outcome.execution_state == "failed" for outcome in outcomes)
    assert all(outcome.side_effect_state == "none" for outcome in outcomes)
    assert all(
        outcome.failure.code == "operation_interrupted" for outcome in outcomes
    )
    assert run_log.pending_tool_calls() == ()


def test_observation_batch_does_not_swallow_process_interrupts(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall("list_files", {"path": "."}, "call_a"),
        ToolCall("list_files", {"path": "."}, "call_b"),
    )
    batch = agent.apply_run_event(run_log.append_tool_batch(calls))

    def interrupted(_context, _args):
        raise KeyboardInterrupt

    monkeypatch.setitem(tools_module._TOOL_RUNNERS, "list_files", interrupted)

    with pytest.raises(KeyboardInterrupt):
        agent.tools.execute_pending_batch(batch.batch_id)

    assert run_log.pending_tool_calls() == calls


def test_observation_batch_preserves_reported_side_effects(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall("list_files", {"path": "."}, "call_known"),
        ToolCall("list_files", {"path": "."}, "call_unknown"),
    )
    batch = agent.apply_run_event(run_log.append_tool_batch(calls))

    def impure_observation(context, _args):
        if context.tool_call_id() == "call_known":
            return ToolRunnerResult(
                "known effect",
                affected_paths=("changed.txt",),
                effect_scope="workspace",
            )
        return ToolRunnerResult("unknown effect", effect_scope="workspace")

    monkeypatch.setitem(
        tools_module._TOOL_RUNNERS,
        "list_files",
        impure_observation,
    )

    outcomes = agent.tools.execute_pending_batch(batch.batch_id)

    assert [outcome.side_effect_state for outcome in outcomes] == [
        "unknown",
        "unknown",
    ]
    assert outcomes[0].affected_paths == ("changed.txt",)
    assert all(outcome.effect_scope == "workspace" for outcome in outcomes)
    assert all(outcome.status == "partial_success" for outcome in outcomes)
    assert all(
        outcome.failure.code == "observation_reported_side_effect"
        for outcome in outcomes
    )
