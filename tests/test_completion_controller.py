from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.completion_controller import CompletionController
from pico.contracts import ToolCall, ToolRunnerResult
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


def test_completion_does_not_reuse_verification_after_external_edit(tmp_path):
    agent, target = active_agent(tmp_path)
    before = agent.workspace.content_fingerprint(force=True)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
        }
    )
    agent.run.evidence.verifications.append(
        {
            "status": "passed",
            "freshness": "current",
            "workspace_fingerprint": before,
        }
    )
    target.write_text("beta\n", encoding="utf-8")
    calls = []

    def verify(workspace_fingerprint):
        calls.append(True)
        return {
            "status": "passed",
            "freshness": "current",
            "workspace_fingerprint": workspace_fingerprint,
        }

    agent.run_verification = verify

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is True
    assert calls == [True]
    assert agent.run.evidence.verifications[-1]["workspace_fingerprint"] != before


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
    agent.run_verification = lambda _fingerprint: verification_calls.append(True)

    assessment = CompletionController(agent).assess("done")

    assert assessment.allowed is False
    assert assessment.status == "syntax_invalid"
    assert "broken.py" in assessment.instruction
    assert verification_calls == []


def test_failed_verifier_records_result_and_blocks_completion(tmp_path):
    agent, _target = active_agent(tmp_path)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
        }
    )

    def fail_verification(workspace_fingerprint):
        return {
            "status": "failed",
            "freshness": "current",
            "workspace_fingerprint": workspace_fingerprint,
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


def test_successful_matching_shell_command_satisfies_completion_gate(tmp_path):
    agent, _target = active_agent(tmp_path)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
        }
    )
    agent.tools.registry["run_shell"]["run"] = lambda _args: ToolRunnerResult(
        "exit_code: 0\nstdout:\n1 passed"
    )
    call = ToolCall(
        "run_shell",
        {"command": "verify", "timeout": 20},
        "call_verify",
    )
    agent.apply_run_event(agent.run.run_log.append_tool_call(call))

    outcome = agent.tools.run(call)
    agent.run_verification = lambda _fingerprint: (_ for _ in ()).throw(
        AssertionError("matching verification must not run twice")
    )
    assessment = CompletionController(agent).assess("done")

    assert outcome.status == "success"
    assert assessment.allowed is True
    verification = agent.run.evidence.verifications[-1]
    assert verification["source_tool_call_id"] == "call_verify"
    assert verification["status"] == "passed"
