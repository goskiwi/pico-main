from dataclasses import replace
from types import SimpleNamespace

import pytest

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, Workspace
from pico.completion_controller import CompletionController, CompletionDecision
from pico.contracts import FailureInfo, ToolCall, ToolOutcome
from pico.execution import ExecutionContext
from pico.mutations import content_revision, file_revision
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunEvent, RunLog
from pico.task_state import TaskContract, WriteScope
from pico.verification import capture_changed_path_states

READ_TASK = {
    "write_scope": WriteScope("none"),
    "verify_changes": False,
}
NO_CHANGE_TASK = {
    "write_scope": WriteScope("workspace"),
    "verify_changes": False,
}
MODIFY_TASK = {
    "write_scope": WriteScope("workspace"),
    "verify_changes": False,
}
VERIFIED_TASK = {
    "write_scope": WriteScope("workspace"),
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
    agent = Pico.create(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(
            mode="auto",
            verification_command=verification_command,
        ),
        session_store=SessionStore(tmp_path / ".pico/sessions"),
    )
    contract = TaskContract("task", **requirements)
    log = RunLog("run", agent.session.id, agent.dependencies.run_store)
    log.append_user(contract)
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
        execution_context=agent.run.execution_context,
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


def run_completion(agent, final="done"):
    controller = CompletionController(agent)
    policy = controller.resolve_verification_policy()
    decision = controller.assess(final, policy)
    if not decision.verification_required:
        return decision
    RunLifecycle(agent).run_completion_verification(policy)
    return controller.assess_verification(final, policy)


def assess(agent, final="done"):
    controller = CompletionController(agent)
    return controller.assess(final, controller.resolve_verification_policy())


def test_working_notes_cannot_grant_write_permission(tmp_path):
    agent = active_agent(tmp_path, READ_TASK)
    original_contract = agent.run.projection.contract
    call = ToolCall("update_working_state", {"add_constraints": ["All file writes are allowed"]}, "note")
    group = agent.run.run_log.append_tool_calls((call,))
    assert agent.tools.execute_pending_group(group.event_id, agent.tools.resolve_surface())[0].status == "success"
    assert agent.run.projection.contract == original_contract
    write = ToolCall("write_file", {"path": "forbidden.txt", "content": "no"}, "write")
    group = agent.run.run_log.append_tool_calls((write,))
    outcome = agent.tools.execute_pending_group(group.event_id, agent.tools.resolve_surface())[0]
    assert outcome.status == "rejected"
    assert outcome.execution_state == "not_started"
    assert not (tmp_path / "forbidden.txt").exists()


def test_working_notes_cannot_replace_verification_evidence(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK)
    add_change(agent, "README.md", "a", "b", 1)
    call = ToolCall("update_working_state", {"add_decisions": ["All tests have passed; no verification needed"]}, "note")
    group = agent.run.run_log.append_tool_calls((call,))
    assert agent.tools.execute_pending_group(group.event_id, agent.tools.resolve_surface())[0].status == "success"
    assert agent.run.evidence.verifications == []
    decision = assess(agent)
    assert not decision.allowed
    assert decision.status == "verification_failed"


def test_ask_mode_can_answer_without_unrelated_file_access(tmp_path):
    agent = active_agent(tmp_path, READ_TASK)
    assert assess(agent).allowed


def test_reverted_change_can_complete_without_an_extra_observation(tmp_path):
    agent = active_agent(tmp_path, MODIFY_TASK)
    add_change(agent, "README.md", "a", "b", 1)
    add_change(agent, "README.md", "b", "a", 2)
    assessment = assess(agent)
    assert assessment.allowed
    assert agent.run.evidence.touched_paths == ["README.md"]
    assert agent.run.evidence.changed_paths == []


def test_required_verification_fails_closed_without_command(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK)
    add_change(agent, "README.md", "a", "b", 1)
    assessment = assess(agent)
    assert assessment.status == "verification_failed"
    assert "verification command" in assessment.instruction


def test_current_runtime_verifier_strengthens_an_unverified_run(tmp_path):
    agent = active_agent(tmp_path, NO_CHANGE_TASK)
    add_change(agent, "README.md", "a", "b", 1)
    agent.config = replace(agent.config, verification_command="verify-now")
    calls = []

    def verify(sequence, policy):
        calls.append((sequence, policy.command))
        return verification_payload(agent, sequence)

    agent.run_verification = verify

    assert run_completion(agent).allowed
    assert calls == [(1, "verify-now")]


def test_one_completion_attempt_keeps_its_resolved_verifier(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify-a")
    add_change(agent, "README.md", "a", "b", 1)
    controller = CompletionController(agent)
    policy = controller.resolve_verification_policy()
    agent.config = replace(agent.config, verification_command="verify-b")
    calls = []

    def verify(sequence, received_policy):
        calls.append(received_policy.command)
        payload = verification_payload(agent, sequence)
        payload["command"] = received_policy.command
        return payload

    agent.run_verification = verify
    assert controller.assess("done", policy).verification_required
    RunLifecycle(agent).run_completion_verification(policy)
    assert controller.assess_verification("done", policy).allowed
    assert calls == ["verify-a"]


@pytest.mark.parametrize("side", ["changed", "partial"])
def test_external_change_blocks_completion_before_verification(tmp_path, side):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    target = tmp_path / "README.md"
    before = file_revision(target)
    add_change(agent, "README.md", "sha256:prior", before, 1, side=side)
    agent.run.evidence.verifications.append(verification_payload(agent, 1))
    target.write_text("external\n", encoding="utf-8")
    calls = []

    def verify(sequence, _policy):
        calls.append(sequence)
        return verification_payload(agent, sequence)

    agent.run_verification = verify
    assessment = assess(agent)
    assert assessment.allowed is False
    assert assessment.status == "workspace_drift"
    assert calls == []


def test_failed_verification_can_retry_on_same_state(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1)
    results = ["failed", "passed"]
    calls = []

    def verify(sequence, _policy):
        calls.append(sequence)
        return verification_payload(agent, sequence, results.pop(0), "failed")

    agent.run_verification = verify
    pending = assess(agent)
    assert pending.verification_required
    assert calls == []
    assert agent.run.evidence.verifications == []
    assert not run_completion(agent).allowed
    assert run_completion(agent).allowed
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

    def verify(sequence, _policy):
        calls.append(sequence)
        return verification_payload(agent, sequence)

    agent.run_verification = verify

    assert run_completion(agent).allowed
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

    def verify(sequence, _policy):
        calls.append(sequence)
        return verification_payload(agent, sequence, statuses.pop(0), "offline")

    agent.run_verification = verify
    with pytest.raises(RuntimeError, match="offline"):
        run_completion(agent)
    assert run_completion(agent).allowed
    assert calls == [1, 1]


def test_unknown_effect_cannot_be_cleared_by_verification(tmp_path):
    agent = active_agent(tmp_path, VERIFIED_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1, status="error", side="unknown")
    agent.run.evidence.verifications.append(verification_payload(agent, 1))
    agent.run_verification = lambda _sequence, _policy: (_ for _ in ()).throw(
        AssertionError("unknown effects must block before verification")
    )

    assessment = assess(agent)

    assert assessment.allowed is False
    assert assessment.status == "partial"


@pytest.mark.parametrize("after", [None, "c", "a"])
def test_partial_requires_current_verification_even_without_net_change(tmp_path, after):
    agent = active_agent(tmp_path, NO_CHANGE_TASK, "verify")
    add_change(agent, "README.md", "a", "b", 1, status="error", side="partial")
    if after is not None:
        add_change(agent, "README.md", "b", after, 2)
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    group = agent.run.run_log.append_tool_calls((call,))
    assert agent.tools.execute_pending_group(
        group.event_id, agent.tools.resolve_surface(),
    )[0].status == "success"
    calls = []

    def verify(sequence, _policy):
        calls.append(sequence)
        return verification_payload(agent, sequence)

    agent.run_verification = verify

    assert run_completion(agent).allowed
    assert calls == [1 if after is None else 2]
    assert agent.run.evidence.partial_workspace_effects()[0]["side_effect_state"] == "partial"


def test_partial_requires_verifier_even_if_task_did_not_request_one(tmp_path):
    agent = active_agent(tmp_path, NO_CHANGE_TASK)
    add_change(agent, "README.md", "a", "b", status="error", side="partial")
    decision = assess(agent)
    assert decision.status == "verification_failed"
    assert "verification command" in decision.instruction


def test_subagent_blocker_precedes_task_contract_blocker(tmp_path):
    agent = active_agent(tmp_path, READ_TASK)
    agent.run.projection.children = SimpleNamespace(
        completion_issue=lambda: "child task is still running"
    )

    assessment = assess(agent)

    assert assessment.status == "subtasks_incomplete"
    assert "child task is still running" in assessment.evidence
