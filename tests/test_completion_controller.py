import pytest

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.completion_controller import CompletionController
from pico.contracts import FailureInfo, ToolCall, ToolRunnerResult
from pico.mutations import file_revision
from pico.run_log import RunLog
from pico.task_state import TaskState


def active_agent(tmp_path):
    target = tmp_path / "subject.txt"
    target.write_text("alpha\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto", verification_command="verify"),
    )
    state = TaskState.create("task_verify", "verify", run_id="run_verify")
    agent.run.task_state = state
    agent.run.run_log = RunLog(
        state.run_id,
        state.task_id,
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    agent.run.run_log.append_user(state.working_state.goal)
    return agent, target


def test_completion_freshness_ignores_external_edits_without_runtime_events(
    tmp_path,
):
    agent, target = active_agent(tmp_path)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
            "event_sequence": 0,
        }
    )
    agent.run.evidence.verifications.append(
        {
            "status": "passed",
            "freshness": "current",
            "workspace_mutation_sequence": 0,
        }
    )
    target.write_text("beta\n", encoding="utf-8")
    agent.run_verification = lambda _sequence: (_ for _ in ()).throw(
        AssertionError("external edits are outside Runtime mutation freshness")
    )

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is True


def test_invalid_changed_python_blocks_completion_before_verification(tmp_path):
    agent, _target = active_agent(tmp_path)
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["broken.py"],
        }
    )
    verification_calls = []
    agent.run_verification = lambda _sequence: verification_calls.append(True)

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is False
    assert assessment.status == "syntax_invalid"
    assert "broken.py" in assessment.instruction
    assert verification_calls == []


def test_later_runtime_mutation_invalidates_and_reruns_verification(tmp_path):
    agent, target = active_agent(tmp_path)

    def edit(call_id, old_text, new_text):
        call = ToolCall(
            "edit_file",
            {
                "path": "subject.txt",
                "old_text": old_text,
                "new_text": new_text,
                "expected_revision": file_revision(target),
            },
            call_id,
        )
        agent.apply_run_event(agent.run.run_log.append_tool_call(call))
        return agent.tools.run(call)

    assert edit("call_first_edit", "alpha", "beta").status == "success"
    first_cursor = agent.run.evidence.last_workspace_mutation_sequence
    agent.emit_event(
        "verification_result",
        {
            "status": "passed",
            "freshness": "current",
            "workspace_mutation_sequence": first_cursor,
        },
    )

    assert edit("call_second_edit", "beta", "gamma").status == "success"
    second_cursor = agent.run.evidence.last_workspace_mutation_sequence
    assert second_cursor > first_cursor
    assert agent.run.evidence.verifications[0]["freshness"] == "stale"
    calls = []

    def verify(sequence):
        calls.append(sequence)
        return {
            "status": "passed",
            "freshness": "current",
            "workspace_mutation_sequence": sequence,
        }

    agent.run_verification = verify

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is True
    assert calls == [second_cursor]


def test_failed_verifier_records_result_and_blocks_completion(tmp_path):
    agent, _target = active_agent(tmp_path)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
        }
    )

    def fail_verification(workspace_mutation_sequence):
        return {
            "status": "failed",
            "freshness": "current",
            "workspace_mutation_sequence": workspace_mutation_sequence,
            "output": "1 failed",
        }

    agent.run_verification = fail_verification

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is False
    assert assessment.status == "verification_failed"
    assert "1 failed" in assessment.instruction
    verification_events = [
        entry
        for entry in agent.run.run_log.events
        if entry.kind == "verification_result"
    ]
    assert len(verification_events) == 1
    assert verification_events[0].payload["status"] == "failed"


def test_verifier_infrastructure_error_stops_instead_of_looping(tmp_path):
    agent, _target = active_agent(tmp_path)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
        }
    )
    agent.run_verification = lambda workspace_mutation_sequence: {
        "status": "infrastructure_error",
        "freshness": "current",
        "workspace_mutation_sequence": workspace_mutation_sequence,
        "output": "docker unavailable",
    }

    with pytest.raises(RuntimeError, match="docker unavailable"):
        CompletionController(agent).assess("done")

    events = [
        entry for entry in agent.run.run_log.events
        if entry.kind == "verification_result"
    ]
    assert len(events) == 1


def test_successful_matching_shell_command_satisfies_completion_gate(tmp_path):
    agent, _target = active_agent(tmp_path)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
        }
    )
    agent.tools.registry["run_shell"]["run"] = lambda _args: ToolRunnerResult(
        "exit_code: 0\nstdout:\n1 passed",
        structured={"exit_code": 0},
    )
    call = ToolCall(
        "run_shell",
        {"command": "verify", "timeout_seconds": 20},
        "call_verify",
    )
    agent.apply_run_event(agent.run.run_log.append_tool_call(call))

    outcome = agent.tools.run(call)
    agent.run_verification = lambda _sequence: (_ for _ in ()).throw(
        AssertionError("matching verification must not run twice")
    )
    assessment = CompletionController(agent).assess("done")

    assert outcome.status == "success"
    assert assessment.allowed is True
    verification = agent.run.evidence.verifications[-1]
    assert verification["source_tool_call_id"] == "call_verify"
    assert verification["status"] == "passed"
    assert verification["started_workspace_mutation_sequence"] == verification[
        "workspace_mutation_sequence"
    ]


def test_failed_matching_shell_command_records_current_verification_failure(tmp_path):
    agent, _target = active_agent(tmp_path)
    agent.tools.registry["run_shell"]["run"] = lambda _args: ToolRunnerResult(
        "exit_code: 1\nstderr:\n1 failed",
        structured={"exit_code": 1},
        failure=FailureInfo(
            "command_failed",
            "command exited with 1",
            "retry_after_change",
        ),
    )
    call = ToolCall(
        "run_shell",
        {"command": "verify", "timeout_seconds": 20},
        "call_failed_verify",
    )
    agent.apply_run_event(agent.run.run_log.append_tool_call(call))

    outcome = agent.tools.run(call)
    verification = agent.run.evidence.verifications[-1]

    assert outcome.status == "error"
    assert verification["status"] == "failed"
    assert verification["freshness"] == "current"
    assert verification["exit_code"] == 1
