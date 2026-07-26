import json
from types import SimpleNamespace

import pytest

import pico.cli as cli
from pico.run_undo import (
    RunUndoConflictError,
    RunUndoError,
    RunUndoJournal,
    restore_run,
)
from tests.fakes import final_action, tool_action_json
from tests.helpers import build_agent


def _run_id(agent):
    return agent.current_task_state.run_id


def test_run_undo_restores_dirty_preimage_and_removes_created_paths(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            tool_action_json('{"name":"write_file","args":{"path":"README.md","content":"agent version\\n"}}'),
            tool_action_json('{"name":"write_file","args":{"path":"generated/out.txt","content":"new\\n"}}'),
            final_action("Changed two files."),
        ],
    )
    (tmp_path / "README.md").write_text(
        "user dirty version\n",
        encoding="utf-8",
    )

    assert agent.ask("Make the requested changes") == "Changed two files."
    run_id = _run_id(agent)
    report_path = agent.run_store.report_path(run_id)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "agent version\n"
    assert (tmp_path / "generated" / "out.txt").is_file()
    assert report["undo"]["available"] is True
    assert report["undo"]["changed_paths"] == [
        "README.md",
        "generated",
        "generated/out.txt",
    ]
    assert report["tool_audit"][0]["undo_status"] == "recorded"
    assert report["tool_audit"][0]["undo_recorded_paths"] == ["README.md"]

    result = restore_run(tmp_path, run_id)

    assert result.restored_paths == (
        "README.md",
        "generated",
        "generated/out.txt",
    )
    assert result.deleted_paths == ("generated", "generated/out.txt")
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "user dirty version\n"
    assert not (tmp_path / "generated").exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["undo"]["status"] == "restored"
    assert json.loads(
        agent.run_store.trace_path(run_id)
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )["event"] == "undo_applied"


def test_run_undo_conflict_refuses_every_path_before_writing(tmp_path):
    (tmp_path / "one.txt").write_text("one before\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two before\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            tool_action_json('{"name":"write_file","args":{"path":"one.txt","content":"one agent\\n"}}'),
            tool_action_json('{"name":"write_file","args":{"path":"two.txt","content":"two agent\\n"}}'),
            final_action("Done."),
        ],
    )

    assert agent.ask("Change both files") == "Done."
    (tmp_path / "two.txt").write_text("two user after run\n", encoding="utf-8")

    with pytest.raises(RunUndoConflictError) as exc_info:
        restore_run(tmp_path, _run_id(agent))

    assert exc_info.value.paths == ("two.txt",)
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "one agent\n"
    assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "two user after run\n"


def test_run_undo_validates_all_blobs_before_restoring_any_path(tmp_path):
    (tmp_path / "one.txt").write_text("one before\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two before\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            tool_action_json('{"name":"write_file","args":{"path":"one.txt","content":"one agent\\n"}}'),
            tool_action_json('{"name":"write_file","args":{"path":"two.txt","content":"two agent\\n"}}'),
            final_action("Done."),
        ],
    )
    agent.ask("Change both files")
    run_dir = agent.run_store.run_dir(_run_id(agent))
    manifest = json.loads(
        (run_dir / "undo" / "manifest.json").read_text(encoding="utf-8")
    )
    corrupt_blob = run_dir / "undo" / manifest["entries"]["two.txt"]["original"]["blob"]
    corrupt_blob.write_bytes(b"corrupt")

    with pytest.raises(RunUndoError, match="corrupt undo blob"):
        restore_run(tmp_path, _run_id(agent))

    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "one agent\n"
    assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "two agent\n"


def test_run_undo_captures_arbitrary_shell_changes_and_modes(tmp_path):
    shell_action = tool_action_json(json.dumps(
        {
            "name": "run_shell",
            "args": {
                "command": (
                    "printf 'shell changed\\n' > README.md && "
                    "chmod 600 README.md && mkdir -p generated && "
                    "printf 'new\\n' > generated/out.txt"
                ),
                "timeout": 20,
            },
        }
    ))
    agent = build_agent(
        tmp_path,
        [
            shell_action,
            final_action("Shell mutation complete."),
        ],
    )
    original_mode = (tmp_path / "README.md").stat().st_mode & 0o777

    assert agent.ask("Run the mutation") == "Shell mutation complete."
    report = json.loads(
        agent.run_store.report_path(_run_id(agent)).read_text(encoding="utf-8")
    )

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "shell changed\n"
    assert (tmp_path / "README.md").stat().st_mode & 0o777 == 0o600
    assert "generated" in report["tool_audit"][0]["affected_paths"]
    assert report["tool_audit"][0]["undo_status"] == "recorded"

    restore_run(tmp_path, _run_id(agent))

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"
    assert (tmp_path / "README.md").stat().st_mode & 0o777 == original_mode
    assert not (tmp_path / "generated").exists()


def test_shell_undo_snapshot_never_copies_env_files(tmp_path):
    secret = "undo-secret-must-not-be-copied"
    (tmp_path / ".env.local").write_text(
        f"OPENAI_API_KEY={secret}\n",
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [])
    run_dir = tmp_path / ".pico" / "runs" / "run_secret_test"
    journal = RunUndoJournal(tmp_path, run_dir, "run_secret_test")
    journal.start()

    token = journal.prepare(
        agent,
        "run_shell",
        {"command": "pytest -q"},
        agent.tools["run_shell"],
    )
    pending_dir = run_dir / "undo" / "pending" / token
    snapshot = json.loads(
        (pending_dir / "snapshot.json").read_text(encoding="utf-8")
    )
    pending_bytes = b"".join(
        path.read_bytes()
        for path in (pending_dir / "blobs").glob("*")
    )

    assert ".env.local" not in snapshot["states"]
    assert secret.encode() not in pending_bytes
    journal.record(token)


def test_undo_prefers_an_explicit_workspace_run_store_over_an_ancestor_repo(
    tmp_path, monkeypatch
):
    fixture_root = tmp_path / "fixture"
    (fixture_root / ".pico" / "runs").mkdir(parents=True)
    captured = {}

    def fake_restore_run(workspace_root, run_id, *, dry_run):
        captured.update(
            {
                "workspace_root": workspace_root,
                "run_id": run_id,
                "dry_run": dry_run,
            }
        )
        return SimpleNamespace(
            already_restored=False,
            dry_run=dry_run,
            run_id=run_id,
            restored_paths=(),
            deleted_paths=(),
        )

    monkeypatch.setattr(cli, "restore_run", fake_restore_run)
    monkeypatch.setattr(
        cli.WorkspaceContext,
        "build",
        lambda _: SimpleNamespace(repo_root=tmp_path / "ancestor-repo"),
    )

    assert cli.run_undo_command(
        ["--cwd", str(fixture_root), "--run", "run_demo", "--dry-run"]
    ) == 0

    assert captured == {
        "workspace_root": fixture_root.resolve(),
        "run_id": "run_demo",
        "dry_run": True,
    }
