import json
import os
import shlex
import shutil
import subprocess
import sys
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
from pico.run_cli import run_main
from pico.run_lifecycle import AgentLoopState, RunLifecycle
from pico.run_log import RunLog
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
    run_log.append_user(
        TaskContract(
            goal=goal,
            allows_workspace_mutation=True,
            verify_changes=False,
        )
    )
    agent.run.projection = run_log.projection
    agent.run.run_log = run_log
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    return run_log


def run_active(agent, call):
    run_log = agent.run.run_log or start_run(agent)
    group = run_log.append_tool_calls((call,))
    return agent.tools.execute_pending_group(group.event_id)[0]


def test_tightened_tool_allowlist_rejects_only_disallowed_group_calls(tmp_path):
    agent = build_agent(tmp_path)
    agent.config = replace(agent.config, allowed_tools=("list_files",))
    assert {tool["name"] for tool in agent.tools.model_action_tools()} == {
        "list_files", "submit_final"
    }
    denied = run_active(agent, ToolCall("read_file", {"path": "README.md"}, "single"))
    assert denied.status == "rejected"
    calls = (ToolCall("read_file", {"path": "README.md"}, "read"),
             ToolCall("list_files", {}, "list"))
    group = agent.run.run_log.append_tool_calls(calls)
    results = agent.tools.execute_pending_group(group.event_id)
    assert [result.execution_state for result in results] == [
        "not_started", "completed"
    ]
    assert [result.status for result in results] == ["rejected", "success"]


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
    effects = replayed.evidence.unverifiable_effects()
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
    agent.run.run_log.append_final("done", final)
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


def test_search_includes_large_files(tmp_path):
    if not shutil.which("rg"):
        pytest.skip("rg is not installed")
    (tmp_path / "large.txt").write_text("padding\n" * 400_000 + "UNIQUE_NEEDLE\n")
    agent = build_agent(tmp_path)
    result = agent.tools.execute_manual("search", {"path": ".", "pattern": "UNIQUE_NEEDLE"})
    assert result.status == "success"
    assert "UNIQUE_NEEDLE" in result.content


def test_search_reports_missing_rg_without_running_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: None)
    agent = build_agent(tmp_path)
    result = agent.tools.execute_manual("search", {"path": ".", "pattern": "needle"})
    assert result.status == "error"
    assert result.failure.code == "search_unavailable"
    assert "ripgrep" in result.content


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
    group = run_log.append_tool_calls((persisted,))

    outcome = agent.tools.execute_pending_group(group.event_id)[0]

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
    run_log.append_tool_calls((call,))

    with pytest.raises(RuntimeError, match="group id does not match"):
        agent.tools.execute_pending_group("call_other")


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
        "model_output",
        "model_artifact_id",
    }
    assert outcome.artifact_id == ""
    assert not (tmp_path / ".pico" / "runs" / "manual" / "artifacts").exists()


def test_complete_model_outcome_is_bounded_and_artifacted(tmp_path):
    agent = build_agent(tmp_path)
    detail = "E" * 100_000
    call = ToolCall("run_command", {"command": "pytest -q"}, "large_failure")

    outcome = agent.tools._outcome(
        call,
        "error",
        "failed",
        "none",
        "short content",
        failure=FailureInfo(
            "command_infrastructure_error",
            detail,
            "retry_after_change",
        ),
        structured={
            "command": "pytest -q",
            "exit_code": 1,
            "stop_reason": "",
            "output_limited": False,
            "repository_changes": [],
        },
    )

    assert outcome.artifact_id == ""
    assert outcome.model_artifact_id.startswith("tool_")
    assert len(outcome.render_for_model().encode("utf-8")) <= 12 * 1024
    full = agent.dependencies.artifacts.read_slice(
        "manual",
        outcome.model_artifact_id,
        0,
        8192,
    )
    assert '"failure"' in full["content"]
    assert full["total_bytes"] > len(outcome.render_for_model())


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
    agent.run.run_log.append_final("created", final_diff)
    replayed = agent.dependencies.run_store.replay("run_tool_test")
    assert replayed.final_diff == final_diff


def test_missing_final_diff_blocks_replay_but_not_event_listing(tmp_path, capsys):
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
    agent.run.run_log.append_final("created", final_diff)
    artifact_root = agent.dependencies.run_store.artifact_dir("run_tool_test")
    (artifact_root / f"{final_diff.artifact_id}.txt").unlink()

    with pytest.raises(ValueError, match="internal artifact is missing"):
        agent.dependencies.run_store.load_run("run_tool_test")
    with pytest.raises(ValueError, match="internal artifact is missing"):
        agent.dependencies.run_store.replay("run_tool_test")
    with pytest.raises(ValueError, match="internal artifact is missing"):
        run_main(["show", "run_tool_test", "--cwd", str(tmp_path)])

    assert run_main(["events", "run_tool_test", "--cwd", str(tmp_path)]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed == [event.to_dict() for event in agent.run.run_log.events]


def test_each_mutation_saves_its_preimage_and_retains_the_run_original(tmp_path):
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
    assert second_artifact.startswith("preimage_")
    assert agent.dependencies.artifacts.read_internal_text("run_tool_test", second_artifact) == "beta\n"
    assert agent.run.evidence.change_set.files["subject.txt"].first_before_artifact_id == first_artifact
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
    assert repeated.failure.code == "existing_file_requires_edit"
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
        or any(call.call_id == "call_second_edit" for call in event.tool_calls)
    ]
    assert second_events == ["assistant_tool_calls", "tool_result"]
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
        assert agent.run.projection.contract is None
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


def test_tool_group_runs_concurrently_but_commits_results_in_call_order(
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
    group = run_log.append_tool_calls(calls)
    barrier = threading.Barrier(2)

    def no_rebuild():
        pytest.fail("group execution must reuse the existing tool registry")

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

    outcomes = agent.tools.execute_pending_group(group.event_id)

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


def test_parallel_limit_runs_large_group_in_bounded_waves(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    agent.config = replace(agent.config, max_parallel_tools=2)
    run_log = start_run(agent)
    calls = tuple(
        ToolCall("list_files", {"path": "."}, f"call_{index}")
        for index in range(6)
    )
    group = run_log.append_tool_calls(calls)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def measured_read(_context, _args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return ToolRunnerResult("done")

    monkeypatch.setitem(agent.tools.registry["list_files"], "run", measured_read)

    outcomes = agent.tools.execute_pending_group(group.event_id)

    assert max_active == 2
    assert len(outcomes) == 6
    assert all(outcome.status == "success" for outcome in outcomes)


def test_exclusive_writes_in_group_run_as_separate_ordered_transactions(tmp_path):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall("write_file", {"path": "a.txt", "content": "a\n"}, "call_a"),
        ToolCall("write_file", {"path": "b.txt", "content": "b\n"}, "call_b"),
    )
    group = run_log.append_tool_calls(calls)

    outcomes = agent.tools.execute_pending_group(group.event_id)

    assert [outcome.status for outcome in outcomes] == ["success", "success"]
    assert (tmp_path / "a.txt").read_text() == "a\n"
    assert (tmp_path / "b.txt").read_text() == "b\n"
    assert [
        (event.kind, event.call_id)
        for event in run_log.events
        if event.kind in {"tool_started", "tool_result"}
    ] == [
        ("tool_started", "call_a"),
        ("tool_result", "call_a"),
        ("tool_started", "call_b"),
        ("tool_result", "call_b"),
    ]


def test_cancelled_group_still_closes_every_call(tmp_path):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall("list_files", {"path": "."}, "call_a"),
        ToolCall("list_files", {"path": "."}, "call_b"),
    )
    group = run_log.append_tool_calls(calls)
    agent.run.execution_context.request_stop("user_cancelled")

    outcomes = agent.tools.execute_pending_group(group.event_id)

    assert len(outcomes) == 2
    assert all(outcome.execution_state == "failed" for outcome in outcomes)
    assert all(outcome.side_effect_state == "none" for outcome in outcomes)
    assert all(
        outcome.failure.code == "operation_interrupted" for outcome in outcomes
    )
    assert run_log.pending_tool_calls() == ()


def test_tool_group_does_not_swallow_process_interrupts(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall("list_files", {"path": "."}, "call_a"),
        ToolCall("list_files", {"path": "."}, "call_b"),
    )
    group = run_log.append_tool_calls(calls)

    def interrupted(_context, _args):
        raise KeyboardInterrupt

    monkeypatch.setitem(agent.tools.registry["list_files"], "run", interrupted)

    with pytest.raises(KeyboardInterrupt):
        agent.tools.execute_pending_group(group.event_id)

    assert run_log.pending_tool_calls() == calls


def test_tool_group_preserves_reported_side_effects(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    run_log = start_run(agent)
    calls = (
        ToolCall("list_files", {"path": "."}, "call_known"),
        ToolCall("list_files", {"path": "."}, "call_unknown"),
    )
    group = run_log.append_tool_calls(calls)

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

    outcomes = agent.tools.execute_pending_group(group.event_id)

    assert [outcome.side_effect_state for outcome in outcomes] == [
        "unknown",
        "unknown",
    ]
    assert outcomes[0].affected_paths == ("changed.txt",)
    assert all(outcome.effect_scope == "workspace" for outcome in outcomes)
    assert all(outcome.status == "partial_success" for outcome in outcomes)
    assert all(
        outcome.failure.code == "parallel_tool_reported_side_effect"
        for outcome in outcomes
    )


@pytest.mark.parametrize("restart", [False, True])
def test_retry_after_restoring_file_depends_on_current_revision(tmp_path, restart):
    agent = build_agent(tmp_path)
    RunLifecycle(agent).initialize("Change A to B")
    target = tmp_path / "sample.txt"
    target.write_text("A")
    args = {
        "path": "sample.txt", "old_text": "A", "new_text": "B",
        "expected_revision": file_revision(target),
    }
    runner = agent.tools.registry["edit_file"]["run"]

    def fail_after_write(context, params):
        runner(context, params)
        raise RuntimeError("injected failure after write")

    with patch.dict(agent.tools.registry["edit_file"], run=fail_after_write):
        first = run_active(agent, ToolCall("edit_file", args, "first"))
    assert first.side_effect_state == "partial"
    stale = run_active(agent, ToolCall("edit_file", args, "stale_retry"))
    assert stale.failure.code == "revision_conflict"
    restored = run_active(agent, ToolCall("edit_file", {
        "path": "sample.txt", "old_text": "B", "new_text": "A",
        "expected_revision": file_revision(target),
    }, "restore"))
    assert restored.status == "success"
    assert run_active(agent, ToolCall("read_file", {"path": "sample.txt"}, "read")).status == "success"
    if restart:
        agent = Pico(
            FakeModelClient([]), Workspace.build(tmp_path), config=agent.config,
            session=agent.session.store.load(agent.session.id),
        )
        RunLifecycle(agent).initialize("Continue")
    result = run_active(agent, ToolCall("edit_file", args, "current_retry"))
    assert result.status == "success"
    assert target.read_text() == "B"


def test_successful_command_can_finish_without_an_unrelated_observation(tmp_path):
    agent = build_agent(tmp_path)
    agent.config = replace(agent.config, mode="code")
    agent.model_client.outputs = [
        ModelAction.tool("run_command", {
            "command": shlex.join([sys.executable, "-B", "-c", "print(2 + 2)"]),
        }),
        ModelAction.final("Python returned 4."),
    ]
    with patch("builtins.input", return_value="yes"):
        result = agent.ask("Run Python to calculate 2 + 2 and report the output")
    assert result.status == "completed"
    calls = [
        call.name
        for event in agent.run.run_log.events
        if event.kind == "assistant_tool_calls"
        for call in event.tool_calls
    ]
    assert calls == ["run_command"]
    assert agent.run.evidence.successful_observation_count == 0


def test_redaction_preserves_machine_paths_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "admin")
    agent = build_agent(tmp_path)
    result = run_active(agent, ToolCall("write_file", {
        "path": "admin.py", "content": "admin\n",
    }, "write"))
    assert result.status == "success"
    assert result.affected_paths == ("admin.py",)
    assert result.structured["path_transitions"][0]["path"] == "admin.py"
    assert "admin" not in result.content
    assert "<redacted>" in result.content
    restored = agent.dependencies.run_store.replay(agent.run.projection.run_id)
    assert restored.evidence.changed_paths == ["admin.py"]
    revision = file_revision(tmp_path / "admin.py")
    secret = revision[-4:]
    monkeypatch.setenv("DB_PASSWORD", secret)
    entry = agent.emit_event("verification_result", {
        "status": "passed", "command": "verify", "output": "password=" + secret,
        "started_workspace_mutation_sequence": 1, "finished_workspace_mutation_sequence": 1,
        "started_changed_path_states": {"admin.py": revision},
        "finished_changed_path_states": {"admin.py": revision}, "workspace_changes": [],
    })
    assert entry.payload["finished_changed_path_states"] == {"admin.py": revision}
    assert entry.payload["output"] == "password=<redacted>"


def test_invalid_result_cannot_poison_the_durable_log(tmp_path):
    agent = build_agent(tmp_path)
    log = start_run(agent)
    call = ToolCall("write_file", {"path": "a.txt", "content": "a"}, "write")
    log.append_tool_calls((call,))
    log.append_tool_started(call, effect_scope="workspace", potential_effects=[])
    before = agent.dependencies.run_store.events_path(log.run_id).read_bytes()
    invalid = ToolOutcome("write", "write_file", "success", "completed", "changed", "written",
                          affected_paths=("a.txt",), effect_scope="workspace")
    with pytest.raises(ValueError, match="path transitions"):
        log.append_tool_result(invalid)
    assert agent.dependencies.run_store.events_path(log.run_id).read_bytes() == before
    restored = agent.dependencies.run_store.replay(log.run_id)
    assert restored.pending_call_ids == ("write",)
    assert not restored.evidence.changed_paths


def test_preimage_and_commit_revision_cannot_disagree(tmp_path):
    agent = build_agent(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("B\n")
    read = run_active(agent, ToolCall("read_file", {"path": "target.txt"}, "read"))
    target.write_text("A\n")
    capture = agent.tools._preimage_artifacts

    def external_editor_interleaving(*args):
        try:
            return capture(*args)
        finally:
            target.write_text("B\n")

    with patch.object(agent.tools, "_preimage_artifacts", external_editor_interleaving):
        result = run_active(agent, ToolCall("edit_file", {
            "path": "target.txt", "old_text": "B", "new_text": "C",
            "expected_revision": read.structured["revision"],
        }, "edit"))
    assert result.failure.code == "revision_conflict"
    assert target.read_text() == "B\n"
    assert not agent.run.evidence.changed_paths


def test_rg_search_handles_later_files_and_backtracking_patterns(tmp_path):
    if not shutil.which("rg"):
        pytest.skip("rg is not installed")
    (tmp_path / "a.txt").write_text("a" * 10000 + "!")
    (tmp_path / "b.txt").write_text("needle\n")
    agent = build_agent(tmp_path)
    result = agent.tools.execute_manual("search", {"pattern": "needle"})
    assert result.status == "success" and "b.txt:1:needle" in result.content
    started = time.monotonic()
    result = agent.tools.execute_manual("search", {"pattern": "(a+)+$"})
    assert result.status == "success"
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("cancel", [False, True])
def test_search_consumes_request_deadline_and_cancellation(tmp_path, cancel):
    executable = tmp_path / "slow-rg"
    executable.write_text(f"#!{sys.executable}\nimport time\nprint('a.txt:1:needle', flush=True)\ntime.sleep(2)\n")
    executable.chmod(0o755)
    execution = ExecutionContext.root(max_seconds=10 if cancel else 0.15)
    timer = threading.Timer(0.15, lambda: execution.request_stop("user_cancelled"))
    if cancel:
        timer.start()
    try:
        started = time.monotonic()
        result = tools_module._bounded_rg_search(tmp_path, ".", "needle", str(executable), execution)
        assert time.monotonic() - started < 1.5
        assert result.failure.code == ("operation_interrupted" if cancel else "search_timeout")
    finally:
        timer.cancel()


def test_utf8_artifact_pages_advance_or_reject_small_capacity(tmp_path):
    agent = build_agent(tmp_path)
    log = start_run(agent)
    text = "中文🙂abc"
    descriptor = agent.dependencies.artifacts.write_tool_output(log.run_id, "output", text)
    store = agent.dependencies.artifacts
    with pytest.raises(ValueError, match="at least 4"):
        store.read_slice(log.run_id, descriptor["artifact_id"], 0, 1)
    offset, chunks = 0, []
    while offset < len(text.encode()):
        page = store.read_slice(log.run_id, descriptor["artifact_id"], offset, 4)
        assert page["end_offset"] > offset
        offset = page["end_offset"]
        chunks.append(page["content"])
    assert "".join(chunks) == text
