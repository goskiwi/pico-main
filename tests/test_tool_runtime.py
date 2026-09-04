import os
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from pico.delivery import build_final_diff
from pico.execution import ExecutionContext
from pico.mutations import file_revision
from pico.run_lifecycle import AgentLoopState, RunLifecycle
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    runtime_workspace = Workspace.build(tmp_path)
    return Pico(
        model_client=FakeModelClient([]),
        workspace=runtime_workspace,
        config=PicoConfig(mode="auto"),
        session=SessionStore(tmp_path / ".pico" / "sessions").create(
            runtime_workspace.root
        ),
    )


def start_run(agent, *, run_id="run_tool_test", goal="Exercise tools"):
    run_log = RunLog(
        run_id,
        "task_tool_test",
        agent.session.id,
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


def test_tightened_tool_allowlist_applies_to_model_single_and_batch(tmp_path):
    agent = build_agent(tmp_path)
    agent.config = replace(agent.config, allowed_tools=("list_files",))
    assert {tool["name"] for tool in agent.tools.model_action_tools()} == {
        "list_files", "submit_final"
    }
    denied = run_active(agent, ToolCall("read_file", {"path": "README.md"}, "single"))
    assert denied.status == "rejected"
    calls = (ToolCall("read_file", {"path": "README.md"}, "read"),
             ToolCall("list_files", {}, "list"))
    batch = agent.apply_run_event(agent.run.run_log.append_tool_batch(calls))
    results = agent.tools.execute_pending_batch(batch.batch_id)
    assert all(result.execution_state == "not_started" for result in results)


def test_verification_side_effect_survives_resubmission_and_replay(tmp_path):
    agent = build_agent(tmp_path)
    agent.config = replace(agent.config, verification_command=(
        "test -f generated.txt || printf generated > generated.txt"
    ))
    agent.model_client.outputs = [
        ModelAction.tool("write_file", {"path": "created.txt", "content": "created\n"}),
        ModelAction.final("done"), ModelAction.final("done"), ModelAction.final("done"),
    ]
    outcome = agent.ask("Create created.txt only")
    assert outcome.status == "stopped"
    assert len(agent.run.evidence.verifications) == 1
    replayed = agent.dependencies.run_store.replay(outcome.run_id)
    effects = replayed.evidence.unrepaired_uncertain_effects()
    assert any("generated.txt" in effect["affected_paths"] for effect in effects)


def test_each_single_call_receives_its_own_current_context(tmp_path):
    agent = build_agent(tmp_path)
    contexts = []

    def observe(context, _args):
        contexts.append(context)
        return ToolRunnerResult("observed")

    agent.tools.registry["list_files"]["run"] = observe
    run_active(agent, ToolCall("list_files", {}, "first"))
    agent.run.execution_context = None
    agent.reset()
    start_run(agent, run_id="run_second")
    run_active(agent, ToolCall("list_files", {}, "second"))

    first, second = contexts
    assert first is not second
    assert first.tool_call_id == "first" and second.tool_call_id == "second"
    assert first.run_id == "run_tool_test" and second.run_id == "run_second"
    assert first.working_state is not second.working_state
    assert first.execution_context is not second.execution_context


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_large_dirty_file_read_edit_final_diff_and_replay(tmp_path, newline):
    target = tmp_path / "large.txt"
    tail = ("context" + newline) * 10 + "x" * (9 * 1024 * 1024) + newline
    target.write_bytes(("committed" + newline + tail).encode())
    for args in (
        ["init", "-q"], ["add", "large.txt"],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-qm", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    target.write_bytes(("用户修改" + newline + tail).encode())
    agent = build_agent(tmp_path)
    observed = run_active(agent, ToolCall("read_file", {
        "path": "large.txt", "start_line": 1, "end_line": 1,
    }, "read_large"))
    assert observed.status == "success"
    assert "用户修改" in observed.content
    assert observed.structured["revision"] == file_revision(target)

    edited = run_active(agent, ToolCall("edit_file", {
        "path": "large.txt", "old_text": "用户修改", "new_text": "Pico 修改",
        "expected_revision": observed.structured["revision"],
    }, "edit_large"))
    assert edited.status == "success"
    final = build_final_diff(agent)
    diff = agent.dependencies.artifacts.read_internal_text("run_tool_test", final.artifact_id)
    assert "-用户修改" in diff and "+Pico 修改" in diff
    assert "committed" not in diff
    assert len(diff) < 2000  # Unchanged CRLF lines must not become changes.
    assert target.read_bytes() == ("Pico 修改" + newline + tail).encode()
    agent.apply_run_event(agent.run.run_log.append_final("done", final))
    replayed = agent.dependencies.run_store.replay("run_tool_test")
    assert replayed.final_diff == final
    assert replayed.evidence.change_set.render_final_diff(
        tmp_path, agent.dependencies.artifacts, "run_tool_test"
    ) == diff


def test_read_long_unicode_line_bounds_output_and_keeps_full_revision(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("汉字" * 1_500_000 + "\nlast\n")
    agent = build_agent(tmp_path)
    read = agent.tools.registry["read_file"]["run"]
    result = read(agent.tools.context(call_id="probe"), {"path": "long.txt", "start_line": 1, "end_line": 2})
    assert result.structured["truncated"]
    assert result.structured["has_more"]
    assert result.structured["revision"] == file_revision(target)
    assert result.structured["total_lines"] == 2
    assert len(result.content.encode()) < tools_module.READ_FILE_MAX_OUTPUT_BYTES + 512
    assert "read output truncated" in result.content
    last = read(agent.tools.context(call_id="probe"), {"path": "long.txt", "start_line": 2, "end_line": 2})
    assert "2: last" in last.content
    assert not last.structured["truncated"]


@pytest.mark.parametrize("engine", ["rg", "python"])
def test_search_includes_large_files(tmp_path, monkeypatch, engine):
    if engine == "rg" and not shutil.which("rg"):
        pytest.skip("rg is not installed")
    if engine == "python":
        monkeypatch.setattr(tools_module.shutil, "which", lambda _name: None)
    (tmp_path / "large.txt").write_text("padding\n" * 400_000 + "UNIQUE_NEEDLE\n")
    agent = build_agent(tmp_path)
    result = agent.tools.execute_manual("search", {"path": ".", "pattern": "UNIQUE_NEEDLE"})
    assert result.status == "success"
    assert "UNIQUE_NEEDLE" in result.content


def test_fallback_search_bounds_long_matching_output(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: None)
    (tmp_path / "large.txt").write_text("needle" + "x" * 3_000_000)
    agent = build_agent(tmp_path)
    result = agent.tools.registry["search"]["run"](agent.tools.context(call_id="probe"), {"path": ".", "pattern": "needle"})
    assert result.structured["truncated"]
    assert len(result.content.encode()) < tools_module.SEARCH_MAX_OUTPUT_BYTES + 128


def test_preimage_copies_bytes_in_chunks_and_is_immutable(tmp_path, monkeypatch):
    source = tmp_path / "original.txt"
    content = "汉字\r\n".encode() * 400_000
    source.write_bytes(content)
    agent = build_agent(tmp_path)
    original_open = Path.open
    sizes = []

    def checked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path != source:
            return handle
        proxy = MagicMock(wraps=handle)
        proxy.__enter__.return_value = proxy
        proxy.__exit__.side_effect = handle.__exit__

        def read(size=-1):
            assert 0 < size <= 1024 * 1024
            sizes.append(size)
            return handle.read(size)

        proxy.read.side_effect = read
        return proxy

    monkeypatch.setattr(Path, "open", checked_open)
    store = agent.dependencies.artifacts
    descriptor = store.write_workspace_preimage("manual", "copy", "original.txt", source)
    assert store.write_workspace_preimage("manual", "copy", "original.txt", source) == descriptor
    assert len(sizes) > 2
    assert store.read_internal("manual", descriptor["artifact_id"])[1] == content
    root = agent.dependencies.run_store.artifact_dir("manual")
    assert not list(root.glob("*.tmp"))
    (root / f"{descriptor['artifact_id']}.txt").write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="immutable artifact collision"):
        store.write_workspace_preimage("manual", "copy", "original.txt", source)


def test_preimage_publication_failure_does_not_modify_source(tmp_path, monkeypatch):
    target = tmp_path / "original.txt"
    target.write_text("before\n")
    agent = build_agent(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr("pico.artifacts.os.link", fail)
    outcome = run_active(agent, ToolCall("edit_file", {
        "path": "original.txt", "old_text": "before", "new_text": "after",
        "expected_revision": file_revision(target),
    }, "backup_failure"))
    assert outcome.execution_state == "not_started"
    assert outcome.failure.code == "effect_planning_failed"
    assert target.read_text() == "before\n"
    root = agent.dependencies.run_store.artifact_dir("run_tool_test")
    assert not list(root.glob("preimage_*"))
    assert not list(root.glob("*.tmp"))




def code_command_agent(tmp_path, *, approved=True):
    agent = build_agent(tmp_path)
    agent.config = replace(agent.config, mode="code")
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
        runtime_workspace = Workspace.build(tmp_path)
        agent = Pico(
            client,
            runtime_workspace,
            config=PicoConfig(
                mode="auto",
                verification_command="",
                secret_env_names=frozenset({"CUSTOM_SECRET_NAME"}),
            ),
            session=SessionStore(tmp_path / ".pico" / "sessions").create(
                runtime_workspace.root
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

        def fail_with_secret(_context, _args):
            raise RuntimeError(secret)

        agent.tools.registry["list_files"]["run"] = fail_with_secret
        failed = agent.tools.execute_manual("list_files", {})

        (tmp_path / "state-change.txt").write_text("changed\n", encoding="utf-8")
        agent.tools.registry["list_files"]["run"] = lambda _context, _args: ToolRunnerResult(
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
    final_diff = build_final_diff(agent)
    assert final_diff.artifact_id.startswith("diff_")
    assert final_diff.size_bytes > 0
    assert "+alpha" in agent.dependencies.artifacts.read_internal_text(
        "run_tool_test", final_diff.artifact_id
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
    final_diff = build_final_diff(agent)
    agent.apply_run_event(agent.run.run_log.append_final("created", final_diff))
    artifact_root = agent.dependencies.run_store.artifact_dir("run_tool_test")
    (artifact_root / f"{final_diff.artifact_id}.txt").unlink()

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

    def write_then_fail(_context, args):
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

    def write_then_raise(_context, args):
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
    assert replayed.final_diff is None
    assert agent.session.active_run_id == ""
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

    def no_rebuild():
        pytest.fail("batch execution must reuse the existing tool registry")

    monkeypatch.setattr(tools_module, "build_tool_registry", no_rebuild)
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
            call_ids_by_path[args["path"]] = context.tool_call_id
        barrier.wait(timeout=2)
        if args["path"] == "a.txt":
            assert b_finished.wait(timeout=2)
        else:
            b_finished.set()
        with counter_lock:
            active -= 1
        return ToolRunnerResult("worker result " + args["path"])

    monkeypatch.setitem(
        agent.tools.registry["read_file"],
        "run",
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

    monkeypatch.setitem(agent.tools.registry["list_files"], "run", interrupted)

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
        if context.tool_call_id == "call_known":
            return ToolRunnerResult(
                "known effect",
                affected_paths=("changed.txt",),
                effect_scope="workspace",
            )
        return ToolRunnerResult("unknown effect", effect_scope="workspace")

    monkeypatch.setitem(
        agent.tools.registry["list_files"],
        "run",
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
