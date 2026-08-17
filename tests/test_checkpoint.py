from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.checkpoint import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_SCHEMA_MISMATCH_STATUS,
    CHECKPOINT_SCHEMA_VERSION,
    current_runtime_identity,
    evaluate_resume_state,
)


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )


def test_current_runtime_identity_captures_execution_contract(tmp_path):
    agent = build_agent(tmp_path, max_steps=9, max_new_tokens=1024, read_only=True)

    identity = current_runtime_identity(agent)

    assert identity["session_id"] == agent.session["id"]
    assert identity["cwd"] == str(tmp_path)
    assert identity["read_only"] is True
    assert identity["max_steps"] == 9
    assert identity["max_new_tokens"] == 1024
    assert identity["provider_context_limit_tokens"] == 64000
    assert identity["provider_conversation_mode"] == "responses-manual-replay-v1"
    assert identity["workspace_fingerprint"] == agent.workspace.fingerprint()
    assert identity["tool_signature"] == agent.tool_signature()
    assert identity["sandbox_identity"]["backend"] == "docker"
    assert identity["verification_command"] == ""


def test_evaluate_resume_state_distinguishes_no_checkpoint_full_valid_and_schema_mismatch(tmp_path):
    agent = build_agent(tmp_path)

    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_NONE_STATUS

    identity = current_runtime_identity(agent)
    checkpoint = {
        "checkpoint_id": "ckpt_valid",
        "parent_checkpoint_id": "",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "created_at": "2026-04-07T10:00:00+00:00",
        "current_goal": "continue",
        "completed": [],
        "excluded": [],
        "current_blocker": "",
        "next_step": "continue",
        "key_files": [],
        "freshness": {},
        "summary": "saved",
        "runtime_identity": identity,
        "task_state": {"status": "stopped"},
        "context_run_id": "run_saved",
        "pending_partial_paths": [],
        "event_cursor": 0,
        "event_hash": "0" * 64,
        "workspace_content_fingerprint": agent.content_workspace_fingerprint(),
    }
    agent.session["checkpoints"] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "current_id": "ckpt_valid",
        "items": {"ckpt_valid": checkpoint},
    }
    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_FULL_VALID_STATUS

    agent.session["checkpoints"]["items"]["ckpt_valid"]["schema_version"] = "old"
    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_SCHEMA_MISMATCH_STATUS
