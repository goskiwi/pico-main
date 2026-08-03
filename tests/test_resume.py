"""Deterministic checkpoint / resume regression coverage."""

import os

import pico.cli as cli
from pico.agent.checkpoints import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_SCHEMA_MISMATCH_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from pico.runtime import Pico
from tests.fakes import FakeModelClient, final_action, tool_action_json
from tests.helpers import UnitTestSandbox, build_agent, build_workspace


def _checkpointed_read_session(tmp_path):
    """Create a real persisted checkpoint with one tracked file summary."""
    tracked = tmp_path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            tool_action_json(
                '{"name":"read_file","args":{"files":[{"path":"tracked.py","start":1,"end":1}]}}'
            ),
            final_action("Initial inspection complete."),
        ],
    )

    assert agent.ask("Inspect tracked.py before resuming.") == "Initial inspection complete."
    checkpoint_id = agent.session["checkpoints"]["current_id"]
    checkpoint = agent.session["checkpoints"]["items"][checkpoint_id]
    assert checkpoint["key_files"] == [
        {
            "path": "tracked.py",
            "freshness": checkpoint["freshness"]["tracked.py"],
        }
    ]
    assert "tracked.py" in agent.session["memory"]["file_summaries"]
    return agent, checkpoint


def _resume(agent, tmp_path, outputs, **kwargs):
    """Resume with the same deterministic runtime dependencies by default."""
    return Pico.from_session(
        model_client=FakeModelClient(outputs),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy=kwargs.pop("approval_policy", "auto"),
        sandbox=kwargs.pop("sandbox", UnitTestSandbox(tmp_path)),
        **kwargs,
    )


def test_from_session_resumes_a_valid_checkpoint_and_renders_it_in_the_prompt(tmp_path):
    agent, checkpoint = _checkpointed_read_session(tmp_path)

    resumed = _resume(agent, tmp_path, [final_action("Resume completed.")])

    assert resumed.session["id"] == agent.session["id"]
    assert resumed.resume_state["status"] == CHECKPOINT_FULL_VALID_STATUS
    assert resumed.ask("Continue the previous task.") == "Resume completed."
    assert resumed.last_prompt_metadata["resume_status"] == CHECKPOINT_FULL_VALID_STATUS
    prompt = resumed.model_client.prompts[0]
    assert "Task checkpoint:" in prompt
    assert f"Current goal: {checkpoint['current_goal']}" in prompt
    assert "Key files: tracked.py" in prompt


def test_from_session_invalidates_changed_file_summary_and_marks_partial_stale(tmp_path):
    agent, _checkpoint = _checkpointed_read_session(tmp_path)
    (tmp_path / "tracked.py").write_text("value = 2\n", encoding="utf-8")

    resumed = _resume(agent, tmp_path, [final_action("Refreshed evidence.")])

    assert resumed.resume_state["status"] == CHECKPOINT_PARTIAL_STALE_STATUS
    assert resumed.resume_state["stale_paths"] == ["tracked.py"]
    assert "tracked.py" not in resumed.memory.to_dict()["file_summaries"]
    assert resumed.ask("Continue after the file changed.") == "Refreshed evidence."
    assert resumed.last_prompt_metadata["resume_status"] == CHECKPOINT_PARTIAL_STALE_STATUS
    assert resumed.last_prompt_metadata["stale_summary_invalidations"] == 1


def test_from_session_marks_changed_runtime_identity_as_workspace_mismatch(tmp_path):
    agent, _checkpoint = _checkpointed_read_session(tmp_path)

    resumed = _resume(
        agent,
        tmp_path,
        [final_action("Reconfigured runtime resumed.")],
        approval_policy="never",
        max_steps=7,
    )

    assert resumed.resume_state["status"] == CHECKPOINT_WORKSPACE_MISMATCH_STATUS
    assert resumed.resume_state["runtime_identity_mismatch_fields"] == [
        "approval_policy",
        "max_steps",
    ]
    assert resumed.ask("Continue with the new runtime policy.") == "Reconfigured runtime resumed."
    assert resumed.last_prompt_metadata["resume_status"] == CHECKPOINT_WORKSPACE_MISMATCH_STATUS
    assert resumed.last_prompt_metadata["runtime_identity_mismatch_fields"] == [
        "approval_policy",
        "max_steps",
    ]


def test_from_session_marks_incompatible_checkpoint_schema(tmp_path):
    agent, _checkpoint = _checkpointed_read_session(tmp_path)
    saved = agent.session_store.load(agent.session["id"])
    checkpoint_id = saved["checkpoints"]["current_id"]
    saved["checkpoints"]["items"][checkpoint_id]["schema_version"] = "legacy-v0"
    agent.session_store.save(saved)

    resumed = _resume(agent, tmp_path, [final_action("Migration required.")])

    assert resumed.resume_state["status"] == CHECKPOINT_SCHEMA_MISMATCH_STATUS
    assert resumed.ask("Attempt to continue.") == "Migration required."
    assert resumed.last_prompt_metadata["resume_status"] == CHECKPOINT_SCHEMA_MISMATCH_STATUS


def test_cli_resume_latest_selects_the_newest_main_session_not_a_delegate(tmp_path, monkeypatch):
    main_agent = build_agent(tmp_path, [])
    delegate_agent = build_agent(
        tmp_path,
        [],
        agent_mode="delegate",
        parent_agent_id=main_agent.agent_id,
    )
    # Make selection order explicit without sleeping or relying on filesystem timing.
    os.utime(main_agent.session_path, (1, 1))
    os.utime(delegate_agent.session_path, (2, 2))

    monkeypatch.setattr(cli, "_build_model_client", lambda args, env=None: FakeModelClient([]))
    monkeypatch.setattr(cli, "DockerSandbox", lambda root, config: UnitTestSandbox(root))
    args = cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--resume", "latest"]
    )

    resumed = cli.build_agent(args)

    assert resumed.session["id"] == main_agent.session["id"]
    assert resumed.session["session_kind"] == "main"


def test_session_store_latest_ignores_invalid_json_when_selecting_main_session(tmp_path):
    agent = build_agent(tmp_path, [])
    corrupt = agent.session_store.root / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    newer_than_main = os.path.getmtime(agent.session_path) + 10
    os.utime(corrupt, (newer_than_main, newer_than_main))

    assert agent.session_store.latest() == agent.session["id"]
