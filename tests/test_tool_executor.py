from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_ledger import ContextLedger
from pico.contracts import ToolCall, ToolOutcome


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_tool_executor_returns_canonical_outcome_and_artifact(tmp_path):
    agent = build_agent(tmp_path)

    outcome = agent.run_tool(ToolCall("read_file", {"path": "README.md", "start": 1, "end": 1}, "call_test"))

    assert isinstance(outcome, ToolOutcome)
    assert outcome.status == "ok"
    assert outcome.execution_state == "completed"
    assert outcome.side_effect_state == "none"
    assert outcome.admission["status"] == "admitted"
    assert len(outcome.attempts) == 1
    assert [stage["stage"] for stage in outcome.admission["stages"]] == [
        "registry", "surface", "schema", "policy", "approval"
    ]
    artifact = tmp_path / ".pico" / "runs" / "manual" / "artifacts" / f"{outcome.artifact_id}.txt"
    assert artifact.read_text(encoding="utf-8") == outcome.content
    assert agent.artifact_store.verify("manual", outcome.artifact_id)["sha256"]


def test_rejected_call_never_enters_execution(tmp_path):
    outcome = build_agent(tmp_path).run_tool(ToolCall("missing", {}, "call_missing"))

    assert outcome.status == "rejected"
    assert outcome.execution_state == "not_started"
    assert outcome.failure.code == "unknown_tool"
    assert outcome.recovery.action == "replan"
    assert outcome.attempts == ()
    assert outcome.admission["stages"][-1]["stage"] == "registry"


def test_artifact_integrity_verification_detects_tampering(tmp_path):
    agent = build_agent(tmp_path)
    outcome = agent.run_tool(ToolCall("list_files", {}, "call_artifact"))
    path = tmp_path / ".pico" / "runs" / "manual" / "artifacts" / f"{outcome.artifact_id}.txt"
    path.write_text("tampered", encoding="utf-8")

    try:
        agent.artifact_store.verify("manual", outcome.artifact_id)
    except ValueError as exc:
        assert "digest mismatch" in str(exc)
    else:
        raise AssertionError("tampered artifact was accepted")


def test_large_tool_output_keeps_full_artifact_and_bounded_outcome(tmp_path):
    target = tmp_path / "large.txt"
    target.write_text(
        "prefix\n" + "x" * 4500 + "middle-only-marker" + "x" * 4500
        + "\nfull-artifact-tail\n"
    )
    agent = build_agent(tmp_path)

    outcome = agent.run_tool(
        ToolCall("read_file", {"path": "large.txt", "start": 1, "end": 20}, "call_large")
    )

    artifact = (
        tmp_path / ".pico" / "runs" / "manual" / "artifacts" / f"{outcome.artifact_id}.txt"
    )
    assert len(outcome.content) < len(artifact.read_text(encoding="utf-8"))
    assert "middle-only-marker" not in outcome.content
    assert "full-artifact-tail" in outcome.content
    assert "full-artifact-tail" in artifact.read_text(encoding="utf-8")
    assert agent.artifact_store.verify("manual", outcome.artifact_id)["size_bytes"] > 8000

def test_memory_write_is_audited_as_control_effect_with_runtime_provenance(tmp_path):
    agent = build_agent(tmp_path)
    ledger = ContextLedger("run_memory", agent.run_store)
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
    agent.context_ledger = ledger
    agent.evidence_ledger.record_verification({"freshness": "current"})

    outcome = agent.run_tool(call)

    card = agent.project_memory.recall("project_release_command.md")
    assert outcome.status == "ok"
    assert outcome.side_effect_state == "changed"
    assert outcome.metadata["effect_scope"] == "project_memory"
    assert outcome.workspace_changed is False
    assert ".pico/memory/cards/project_release_command.md" in outcome.affected_paths
    assert agent.evidence_ledger.changed_paths == []
    assert ".pico/memory/cards/project_release_command.md" in agent.evidence_ledger.control_changed_paths
    assert agent.evidence_ledger.verifications[0]["freshness"] == "current"
    assert card.source_entry_ids == (source.entry_id,)
    assert card.source_tool_call_id == call.call_id


def test_successful_identical_call_is_blocked_until_state_changes(tmp_path):
    agent = build_agent(tmp_path)
    args = {"path": "README.md", "start": 1, "end": 1}

    first = agent.run_tool(ToolCall("read_file", args, "call_read_1"))
    repeated = agent.run_tool(ToolCall("read_file", args, "call_read_2"))
    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")
    after_change = agent.run_tool(ToolCall("read_file", args, "call_read_3"))

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

    agent.all_tools["run_shell"]["run"] = fail
    args = {"command": "true", "timeout": 20}

    first = agent.run_tool(ToolCall("run_shell", args, "call_shell_1"))
    second = agent.run_tool(ToolCall("run_shell", args, "call_shell_2"))
    third = agent.run_tool(ToolCall("run_shell", args, "call_shell_3"))

    assert first.status == "error"
    assert first.recovery.action == "retry"
    assert second.status == "error"
    assert second.recovery.action == "replan"
    assert third.status == "rejected"
    assert len(executions) == 2


def test_partial_side_effect_blocks_blind_identical_replay(tmp_path):
    agent = build_agent(tmp_path)
    executions = []

    def write_then_fail(args):
        executions.append(1)
        (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
        raise RuntimeError("failed after write")

    agent.all_tools["write_file"]["run"] = write_then_fail
    args = {
        "path": "partial.txt",
        "content": "partial\n",
        "expected_revision": "absent",
    }

    first = agent.run_tool(ToolCall("write_file", args, "call_write_1"))
    repeated = agent.run_tool(ToolCall("write_file", args, "call_write_2"))

    assert first.status == "partial_success"
    assert first.side_effect_state == "partial"
    assert repeated.status == "rejected"
    assert "inspect state" in repeated.failure.detail
    assert len(executions) == 1
