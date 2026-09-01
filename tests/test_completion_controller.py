from types import SimpleNamespace

import pytest

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.completion_controller import CompletionController
from pico.contracts import FailureInfo, ToolCall, ToolOutcome
from pico.execution import ExecutionContext
from pico.mutations import content_revision, file_revision
from pico.run_log import RunEvent, RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract
from pico.verification import capture_changed_path_states

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
VERIFIED_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": False,
    "requires_verification": True,
}


def active_agent(tmp_path, requirements, verification_command=""):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command=verification_command,
        ),
    )
    contract = TaskContract("task", **requirements)
    log = RunLog("run", "task", agent.session.data["id"], agent.dependencies.run_store)
    first = log.append_user(contract)
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.run_log = log
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    return agent


def add_change(agent, path, before, after, sequence=1, status="success", side="changed"):
    target = agent.workspace.resolve_path(path)

    def state(value, *, apply=False):
        value = str(value)
        if value == "absent":
            if apply and target.exists():
                target.unlink()
            return value
        if value.startswith("sha256:"):
            return value
        payload = value.encode("utf-8")
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return content_revision(payload)

    before = state(before)
    after = state(after, apply=True)
    outcome = ToolOutcome(
        f"change_{sequence}",
        "edit_file",
        status,
        "completed" if status == "success" else "failed",
        side,
        "changed",
        structured={
            "path_transitions": [
                {
                    "path": path,
                    "before_state": before,
                    "after_state": after,
                    "before_artifact_id": "preimage_synthetic",
                }
            ]
        },
        affected_paths=(path,),
        effect_scope="workspace",
        failure=(
            None
            if status == "success"
            else FailureInfo("partial", "partial", "no_retry")
        ),
    )
    event = RunEvent(
        f"run:event:{sequence:06d}",
        sequence,
        "run",
        "task",
        agent.session.data["id"],
        "tool_result",
        "now",
        {
            "outcome": outcome.to_dict(),
        },
    )
    agent.run.evidence.apply_event(event)


def verification_payload(agent, sequence, status="passed", output=""):
    states = capture_changed_path_states(
        agent.workspace.root,
        agent.run.evidence.changed_paths,
    )
    return {
        "status": status,
        "started_workspace_mutation_sequence": sequence,
        "finished_workspace_mutation_sequence": sequence,
        "started_changed_path_states": states,
        "finished_changed_path_states": dict(states),
        "output": output,
    }


def test_read_only_requires_successful_observation(tmp_path):
    agent = active_agent(tmp_path, READ_TASK)
    assert CompletionController(agent).assess("done").status == "observation_required"
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    agent.apply_run_event(agent.run.run_log.append_tool_call(call))
    assert agent.tools.execute_pending(call.call_id).status == "success"
    assert CompletionController(agent).assess("done").allowed


def test_required_change_uses_final_net_state(tmp_path):
    agent = active_agent(tmp_path, MODIFY_TASK)
    add_change(agent, "README.md", "a", "b", 1)
    add_change(agent, "README.md", "b", "a", 2)
    assessment = CompletionController(agent).assess("done")
    assert assessment.status == "workspace_change_required"
    assert agent.run.evidence.touched_paths == ["README.md"]
    assert agent.run.evidence.changed_paths == []


def test_required_verification_fails_closed_without_command(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK)
    assessment = CompletionController(agent).assess("done")
    assert assessment.status == "verification_failed"
    assert "no verification command" in assessment.instruction


def test_external_change_blocks_completion_before_verification(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    target = tmp_path / "README.md"
    before = file_revision(target)
    add_change(agent, "README.md", "sha256:prior", before, 1)
    agent.run.evidence.verifications.append(verification_payload(agent, 1))
    target.write_text("external\n", encoding="utf-8")
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence)

    agent.run_verification = verify
    assessment = CompletionController(agent).assess("done")
    assert assessment.allowed is False
    assert assessment.status == "workspace_drift"
    assert calls == []


def test_failed_verification_can_retry_on_same_state(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    results = ["failed", "passed"]
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence, results.pop(0), "failed")

    agent.run_verification = verify
    assert not CompletionController(agent).assess("done").allowed
    assert CompletionController(agent).assess("done").allowed
    assert calls == [0, 0]


def test_infrastructure_error_can_retry_after_environment_recovers(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    statuses = ["infrastructure_error", "passed"]
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence, statuses.pop(0), "offline")

    agent.run_verification = verify
    with pytest.raises(RuntimeError, match="offline"):
        CompletionController(agent).assess("done")
    assert CompletionController(agent).assess("done").allowed
    assert calls == [0, 0]


@pytest.mark.parametrize("side", ["unknown", "partial"])
def test_unrepaired_uncertain_effect_does_not_run_a_meaningless_verifier(
    tmp_path,
    side,
):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1, status="error", side=side)
    agent.run_verification = lambda _sequence: (_ for _ in ()).throw(
        AssertionError("unrepaired uncertainty must block before verification")
    )

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is False
    assert assessment.status == "partial"


def test_repaired_partial_requires_current_verification(tmp_path):
    agent = active_agent(tmp_path, NO_CHANGE_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1, status="error", side="partial")
    add_change(agent, "README.md", "b", "c", 2)
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence)

    agent.run_verification = verify

    assert CompletionController(agent).assess("done").allowed
    assert calls == [2]


def test_subagent_blocker_precedes_task_contract_blocker(tmp_path):
    agent = active_agent(tmp_path, READ_TASK)
    agent.dependencies.subagents = SimpleNamespace(
        completion_issue=lambda: "child task is still running"
    )

    assessment = CompletionController(agent).assess("done")

    assert assessment.status == "subtasks_incomplete"
    assert "child task is still running" in assessment.instruction


def test_task_contract_blocker_precedes_uncertain_effects(tmp_path):
    agent = active_agent(tmp_path, MODIFY_TASK, "verify")
    add_change(
        agent,
        "README.md",
        "a",
        "b",
        1,
        status="error",
        side="unknown",
    )
    agent.run_verification = lambda _sequence: (_ for _ in ()).throw(
        AssertionError("TaskContract must block before verification")
    )

    assessment = CompletionController(agent).assess("done")

    assert assessment.status == "workspace_change_required"
