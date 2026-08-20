from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.contracts import ToolCall, ToolExecution, ToolOutcome
from pico.run_journal import RunJournal
from pico.task_state import TaskState


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto"),
    )


def test_tool_executor_returns_canonical_outcome_and_artifact(tmp_path):
    agent = build_agent(tmp_path)

    outcome = agent.tools.run(
        ToolCall("read_file", {"path": "README.md", "start": 1, "end": 1}, "call_test")
    )

    assert isinstance(outcome, ToolOutcome)
    assert outcome.status == "ok"
    assert outcome.execution_state == "completed"
    assert outcome.side_effect_state == "none"
    assert outcome.admission_status == "admitted"
    assert outcome.rejected_at == ""
    assert set(outcome.to_dict()) == {
        "tool_call_id",
        "tool_name",
        "status",
        "execution_state",
        "side_effect_state",
        "content",
        "admission_status",
        "failure",
        "recovery",
        "affected_paths",
        "effect_scope",
        "duration_ms",
        "artifact",
        "output_truncated",
        "policy_stop_requested",
        "rejected_at",
    }
    artifact = tmp_path / ".pico" / "runs" / "manual" / "artifacts" / f"{outcome.artifact_id}.txt"
    assert artifact.read_text(encoding="utf-8") == outcome.content
    assert agent.services.artifacts.verify("manual", outcome.artifact_id)["sha256"]


def test_rejected_call_never_enters_execution(tmp_path):
    outcome = build_agent(tmp_path).tools.run(
        ToolCall("missing", {}, "call_missing")
    )

    assert outcome.status == "rejected"
    assert outcome.execution_state == "not_started"
    assert outcome.failure.code == "unknown_tool"
    assert outcome.recovery.action == "replan"
    assert outcome.admission_status == "rejected"
    assert outcome.rejected_at == "registry"


def test_artifact_integrity_verification_detects_tampering(tmp_path):
    agent = build_agent(tmp_path)
    outcome = agent.tools.run(ToolCall("list_files", {}, "call_artifact"))
    path = tmp_path / ".pico" / "runs" / "manual" / "artifacts" / f"{outcome.artifact_id}.txt"
    path.write_text("tampered", encoding="utf-8")

    try:
        agent.services.artifacts.verify("manual", outcome.artifact_id)
    except ValueError as exc:
        assert "digest mismatch" in str(exc)
    else:
        raise AssertionError("tampered artifact was accepted")


def test_large_tool_output_keeps_full_artifact_and_bounded_outcome(tmp_path):
    target = tmp_path / "large.txt"
    target.write_text(
        "prefix\n" + "x" * 9000 + "middle-only-marker" + "x" * 9000
        + "\nfull-artifact-tail\n"
    )
    agent = build_agent(tmp_path)

    outcome = agent.tools.run(
        ToolCall("read_file", {"path": "large.txt", "start": 1, "end": 20}, "call_large")
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
    assert agent.services.artifacts.verify("manual", outcome.artifact_id)["size_bytes"] > 16000


def test_large_shell_output_keeps_tail_and_points_to_artifact(tmp_path):
    agent = build_agent(tmp_path)
    output = "head-marker\n" + "noise\n" * 5000 + "tail-marker\n"
    agent.tools.registry["run_shell"]["run"] = (
        lambda _args: ToolExecution("exit_code: 0\n" + output)
    )

    outcome = agent.tools.run(
        ToolCall("run_shell", {"command": "true", "timeout": 20}, "call_shell_large")
    )

    assert "head-marker" not in outcome.content
    assert "tail-marker" in outcome.content
    assert "read_artifact" in outcome.content
    artifact = (
        tmp_path / ".pico" / "runs" / "manual" / "artifacts"
        / f"{outcome.artifact_id}.txt"
    )
    assert "head-marker" in artifact.read_text(encoding="utf-8")


def test_tool_runner_rejects_legacy_string_result(tmp_path):
    agent = build_agent(tmp_path)
    agent.tools.registry["list_files"]["run"] = lambda _args: "legacy result"

    outcome = agent.tools.run(ToolCall("list_files", {}, "call_legacy_runner"))

    assert outcome.status == "error"
    assert outcome.failure.detail == "tool runner must return ToolExecution"


def test_memory_write_is_audited_as_control_effect_with_runtime_provenance(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_memory", "Remember", run_id="run_memory")
    agent.run.task_state = state
    ledger = RunJournal(
        state.run_id,
        state.task_id,
        agent.session.data["id"],
        agent.services.run_store,
    )
    ledger.append_user("Remember the release command")
    call = ToolCall(
        "memory_store",
        {
            "action": "create",
            "filename": "project_release_command.md",
            "name": "Release command",
            "description": "Stable command used before release.",
            "type": "project",
            "content": "Run `python -m pytest -q`.",
            "why": "The user explicitly requested this convention.",
            "how_to_apply": "Run it before release.",
            "expires_at": "",
        },
        "call_memory",
    )
    source = ledger.append_tool_call(call)
    agent.run.journal = ledger
    verification = ledger.append(
        "verification_result",
        {"verification_id": "verify_current", "freshness": "current"},
    )
    agent.run.evidence.apply_entry(verification)

    outcome = agent.tools.run(call)

    card = agent.services.project_memory.recall("project_release_command.md")
    assert outcome.status == "ok"
    assert outcome.side_effect_state == "changed"
    assert outcome.effect_scope == "project_memory"
    assert ".pico/memory/cards/project_release_command.md" in outcome.affected_paths
    assert agent.run.evidence.changed_paths == []
    assert (
        ".pico/memory/cards/project_release_command.md"
        in agent.run.evidence.control_changed_paths
    )
    assert agent.run.evidence.verifications[0]["freshness"] == "current"
    assert card.source_entry_ids == (source.entry_id,)
    assert card.source_tool_call_id == call.call_id


def test_successful_identical_call_is_blocked_until_state_changes(tmp_path):
    agent = build_agent(tmp_path)
    args = {"path": "README.md", "start": 1, "end": 1}

    first = agent.tools.run(ToolCall("read_file", args, "call_read_1"))
    repeated = agent.tools.run(ToolCall("read_file", args, "call_read_2"))
    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")
    after_change = agent.tools.run(ToolCall("read_file", args, "call_read_3"))

    assert first.status == "ok"
    assert repeated.status == "rejected"
    assert repeated.failure.code == "repeated_identical_call"
    assert after_change.status == "ok"
    assert "changed" in after_change.content


def test_retryable_execution_error_gets_one_identical_retry(tmp_path):
    agent = build_agent(tmp_path)
    executions = []

    def fail(_args):
        executions.append(1)
        raise RuntimeError("transient executor failure")

    agent.tools.registry["run_shell"]["run"] = fail
    args = {"command": "true", "timeout": 20}

    first = agent.tools.run(ToolCall("run_shell", args, "call_shell_1"))
    second = agent.tools.run(ToolCall("run_shell", args, "call_shell_2"))
    third = agent.tools.run(ToolCall("run_shell", args, "call_shell_3"))

    assert first.status == "error"
    assert first.recovery.action == "retry"
    assert second.status == "error"
    assert second.recovery.action == "replan"
    assert third.status == "rejected"
    assert len(executions) == 2


def test_read_only_tools_do_not_scan_workspace(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    scans = 0
    original = agent.workspace._scan_snapshot

    def counted():
        nonlocal scans
        scans += 1
        return original()

    monkeypatch.setattr(agent.workspace, "_scan_snapshot", counted)
    agent.tools.run(ToolCall("read_file", {"path": "README.md", "start": 1, "end": 1}, "read_1"))
    agent.tools.run(ToolCall("read_file", {"path": "README.md", "start": 1, "end": 2}, "read_2"))

    assert scans == 0


def test_read_only_agent_turn_and_journal_do_not_scan_workspace(
    tmp_path, monkeypatch
):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient(
            [
                ModelAction.tool(
                    "read_file",
                    {"path": "README.md", "start": 1, "end": 1},
                ),
                ModelAction.final("Done."),
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    scans = 0
    original = agent.workspace._scan_snapshot

    def counted():
        nonlocal scans
        scans += 1
        return original()

    monkeypatch.setattr(agent.workspace, "_scan_snapshot", counted)

    assert agent.ask("Read README.md") == "Done."
    assert scans == 0


def test_workspace_mutation_records_exact_path_without_scanning(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    scans = 0
    original = agent.workspace._scan_snapshot

    def counted():
        nonlocal scans
        scans += 1
        return original()

    monkeypatch.setattr(agent.workspace, "_scan_snapshot", counted)
    outcome = agent.tools.run(
        ToolCall(
            "write_file",
            {"path": "created.txt", "content": "created\n", "expected_revision": "absent"},
            "write_1",
        )
    )

    assert scans == 0
    assert outcome.affected_paths == ("created.txt",)
    assert agent.workspace.revision == 1


def test_partial_side_effect_blocks_blind_identical_replay(tmp_path):
    agent = build_agent(tmp_path)
    executions = []

    def write_then_fail(args):
        executions.append(1)
        (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
        raise RuntimeError("failed after write")

    agent.tools.registry["write_file"]["run"] = write_then_fail
    args = {
        "path": "partial.txt",
        "content": "partial\n",
        "expected_revision": "absent",
    }

    first = agent.tools.run(ToolCall("write_file", args, "call_write_1"))
    repeated = agent.tools.run(ToolCall("write_file", args, "call_write_2"))

    assert first.status == "partial_success"
    assert first.side_effect_state == "partial"
    assert repeated.status == "rejected"
    assert "inspect state" in repeated.failure.detail
    assert len(executions) == 1
