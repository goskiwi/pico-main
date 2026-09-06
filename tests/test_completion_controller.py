from dataclasses import replace
from types import SimpleNamespace

import pytest

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, Workspace
from pico.completion_controller import CompletionController, CompletionDecision
from pico.contracts import FailureInfo, ToolCall, ToolOutcome
from pico.execution import ExecutionContext
from pico.mutations import content_revision, file_revision
from pico.run_log import RunEvent, RunLog
from pico.task_state import TaskContract
from pico.verification import capture_changed_path_states

READ_TASK = {
    "allows_workspace_mutation": False,
    "verify_changes": False,
}
NO_CHANGE_TASK = {
    "allows_workspace_mutation": True,
    "verify_changes": False,
}
MODIFY_TASK = {
    "allows_workspace_mutation": True,
    "verify_changes": False,
}
VERIFIED_TASK = {
    "allows_workspace_mutation": True,
    "verify_changes": True,
}


def test_completion_decision_has_an_explicit_status():
    assert CompletionDecision("allowed", "done").allowed
    assert not CompletionDecision("workspace_drift", "inspect files").allowed
    with pytest.raises(ValueError):
        CompletionDecision("", "")


def active_agent(tmp_path, requirements, verification_command=""):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(
            mode="auto",
            verification_command=verification_command,
        ),
        session=SessionStore(tmp_path / ".pico/sessions").create(
            runtime_workspace.root
        ),
    )
    contract = TaskContract("task", **requirements)
    log = RunLog("run", "task", agent.session.id, agent.dependencies.run_store)
    log.append_user(contract)
    agent.run.projection = log.projection
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
        agent.session.id,
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
        "workspace_changes": [],
        "command": agent.config.verification_command,
        "status": status,
        "started_workspace_mutation_sequence": sequence,
        "finished_workspace_mutation_sequence": sequence,
        "started_changed_path_states": states,
        "finished_changed_path_states": dict(states),
        "output": output,
    }


def test_ask_mode_can_answer_without_unrelated_file_access(tmp_path):
    agent = active_agent(tmp_path, READ_TASK)
    assert CompletionController(agent).assess("done").allowed


def test_reverted_change_can_complete_without_an_extra_observation(tmp_path):
    agent = active_agent(tmp_path, MODIFY_TASK)
    add_change(agent, "README.md", "a", "b", 1)
    add_change(agent, "README.md", "b", "a", 2)
    assessment = CompletionController(agent).assess("done")
    assert assessment.allowed
    assert agent.run.evidence.touched_paths == ["README.md"]
    assert agent.run.evidence.changed_paths == []


def test_required_verification_fails_closed_without_command(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK)
    add_change(agent, "README.md", "a", "b", 1)
    assessment = CompletionController(agent).assess("done")
    assert assessment.status == "verification_failed"
    assert "no verification command" in assessment.content


@pytest.mark.parametrize("side", ["changed", "partial"])
def test_external_change_blocks_completion_before_verification(tmp_path, side):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    target = tmp_path / "README.md"
    before = file_revision(target)
    add_change(agent, "README.md", "sha256:prior", before, 1, side=side)
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
    add_change(agent, "README.md", "a", "b", 1)
    results = ["failed", "passed"]
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence, results.pop(0), "failed")

    agent.run_verification = verify
    assert not CompletionController(agent).assess("done").allowed
    assert CompletionController(agent).assess("done").allowed
    assert calls == [1, 1]


def test_verification_command_change_invalidates_passing_result(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify-a")
    add_change(agent, "README.md", "a", "b", 1)
    agent.run.evidence.verifications.append(verification_payload(agent, 1))
    agent.config = replace(
        agent.config,
        verification_command="verify-b",
    )
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence)

    agent.run_verification = verify

    assert CompletionController(agent).assess("done").allowed
    assert calls == [1]
    assert [record["command"] for record in agent.run.evidence.verifications] == [
        "verify-a",
        "verify-b",
    ]


def test_infrastructure_error_can_retry_after_environment_recovers(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1)
    statuses = ["infrastructure_error", "passed"]
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence, statuses.pop(0), "offline")

    agent.run_verification = verify
    with pytest.raises(RuntimeError, match="offline"):
        CompletionController(agent).assess("done")
    assert CompletionController(agent).assess("done").allowed
    assert calls == [1, 1]


def test_unknown_effect_cannot_be_cleared_by_verification(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1, status="error", side="unknown")
    agent.run.evidence.verifications.append(verification_payload(agent, 1))
    agent.run_verification = lambda _sequence: (_ for _ in ()).throw(
        AssertionError("unknown effects must block before verification")
    )

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is False
    assert assessment.status == "partial"


@pytest.mark.parametrize("after", [None, "c", "a"])
def test_partial_requires_current_verification_even_without_net_change(tmp_path, after):
    agent = active_agent(tmp_path, NO_CHANGE_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1, status="error", side="partial")
    if after is not None:
        add_change(agent, "README.md", "b", after, 2)
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    agent.run.run_log.append_tool_call(call)
    assert agent.tools.execute_pending(call.call_id).status == "success"
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return verification_payload(agent, sequence)

    agent.run_verification = verify

    assert CompletionController(agent).assess("done").allowed
    assert calls == [1 if after is None else 2]
    assert agent.run.evidence.partial_workspace_effects()[0]["side_effect_state"] == "partial"


def test_partial_requires_verifier_even_if_task_did_not_request_one(tmp_path):
    agent = active_agent(tmp_path, NO_CHANGE_TASK)
    add_change(agent, "README.md", "a", "b", status="error", side="partial")
    decision = CompletionController(agent).assess("done")
    assert decision.status == "verification_failed"
    assert "no verification command" in decision.content


def test_subagent_blocker_precedes_task_contract_blocker(tmp_path):
    agent = active_agent(tmp_path, READ_TASK)
    agent.run.projection.children = SimpleNamespace(
        completion_issue=lambda: "child task is still running"
    )

    assessment = CompletionController(agent).assess("done")

    assert assessment.status == "subtasks_incomplete"
    assert "child task is still running" in assessment.content
