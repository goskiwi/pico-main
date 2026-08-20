from types import SimpleNamespace

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.completion import CompletionGate
from pico.completion_controller import CompletionController
from pico.task_state import TaskState


def test_completion_does_not_reuse_verification_after_external_edit(tmp_path):
    target = tmp_path / "subject.txt"
    target.write_text("alpha\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(verification_command="verify"),
    )
    state = TaskState.create("task_verify", "verify", run_id="run_verify")
    agent.run.task_state = state
    agent.services.run_store.start_run(state)
    before = agent.workspace.content_fingerprint(force=True)
    agent.run.evidence.effects.append(
        {
            "effect_scope": "workspace",
            "affected_paths": ["subject.txt"],
        }
    )
    agent.run.evidence.verifications.append(
        {
            "verification_id": "verify_old",
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
            "verification_id": "verify_new",
            "status": "passed",
            "freshness": "current",
            "workspace_fingerprint": workspace_fingerprint,
        }

    agent.run_verification = verify
    frame = SimpleNamespace(completion_gate=CompletionGate(), task_state=state)

    assessment = CompletionController(agent).assess(frame, "done")

    assert assessment.allowed is True
    assert calls == [True]
    assert agent.run.evidence.verifications[-1]["verification_id"] == "verify_new"
