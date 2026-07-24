import json
from unittest.mock import patch

import pytest

import pico.cli as cli
from pico.run_undo import (
    RunUndoConflictError,
    RunUndoError,
    RunUndoJournal,
    restore_run,
)
from tests.helpers import build_agent


def _run_id(agent):
    return agent.current_task_state.run_id


def test_run_undo_restores_dirty_preimage_and_removes_created_paths(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"README.md","content":"agent version\\n"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"generated/out.txt","content":"new\\n"}}</tool>',
            "<final>Changed two files.</final>",
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
            '<tool>{"name":"write_file","args":{"path":"one.txt","content":"one agent\\n"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"two.txt","content":"two agent\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )

    assert agent.ask("Change both files") == "Done."
    (tmp_path / "two.txt").write_text("two user after run\n", encoding="utf-8")

    with pytest.raises(RunUndoConflictError) as exc_info:
        restore_run(tmp_path, _run_id(agent))

    assert exc_info.value.paths == ("two.txt",)
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "one agent\n"
    assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "two user after run\n"


def test_run_undo_rejects_new_descendant_inside_created_directory(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"generated/out.txt","content":"agent\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Create output")
    (tmp_path / "generated" / "user.txt").write_text(
        "user after run\n",
        encoding="utf-8",
    )

    with pytest.raises(RunUndoConflictError) as exc_info:
        restore_run(tmp_path, _run_id(agent))

    assert exc_info.value.paths == ("generated",)
    assert (tmp_path / "generated" / "out.txt").is_file()
    assert (tmp_path / "generated" / "user.txt").is_file()


def test_run_undo_validates_all_blobs_before_restoring_any_path(tmp_path):
    (tmp_path / "one.txt").write_text("one before\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two before\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"one.txt","content":"one agent\\n"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"two.txt","content":"two agent\\n"}}</tool>',
            "<final>Done.</final>",
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
    shell_action = "<tool>" + json.dumps(
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
    ) + "</tool>"
    agent = build_agent(
        tmp_path,
        [
            shell_action,
            "<final>Shell mutation complete.</final>",
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


def test_run_undo_dry_run_only_lists_changes(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"README.md","content":"changed\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Change README")

    result = restore_run(tmp_path, _run_id(agent), dry_run=True)

    assert result.dry_run is True
    assert result.restored_paths == ("README.md",)
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "changed\n"
    manifest = json.loads(
        (
            agent.run_store.run_dir(_run_id(agent))
            / "undo"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "available"


def test_run_undo_is_idempotent_after_success(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"README.md","content":"changed\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Change README")

    first = restore_run(tmp_path, _run_id(agent))
    second = restore_run(tmp_path, _run_id(agent))

    assert first.already_restored is False
    assert second.already_restored is True
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"


def test_run_undo_rejects_unsafe_manifest_path(tmp_path):
    outside = tmp_path.parent / "outside-undo-target.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"README.md","content":"changed\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Change README")
    manifest_path = (
        agent.run_store.run_dir(_run_id(agent))
        / "undo"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"]["../outside-undo-target.txt"] = {
        "original": {"kind": "absent"},
        "expected_post": {
            "kind": "file",
            "mode": 0o644,
            "size": 8,
            "sha256": "0" * 64,
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RunUndoError, match="unsafe undo path"):
        restore_run(tmp_path, _run_id(agent))

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_cli_undo_does_not_build_a_model_client(tmp_path, capsys):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"README.md","content":"changed\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Change README")

    with patch("pico.cli.build_agent", side_effect=AssertionError):
        exit_code = cli.main(
            [
                "undo",
                "--cwd",
                str(tmp_path),
                "--run",
                _run_id(agent),
            ]
        )

    assert exit_code == 0
    assert "restored 1 path(s)" in capsys.readouterr().out
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"


def test_cli_undo_conflict_returns_nonzero(tmp_path, capsys):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"README.md","content":"changed\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )
    agent.ask("Change README")
    (tmp_path / "README.md").write_text("user edit\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "undo",
            "--cwd",
            str(tmp_path),
            "--run",
            _run_id(agent),
        ]
    )

    assert exit_code == 1
    assert "undo refused" in capsys.readouterr().err
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "user edit\n"


def test_cli_undo_help_is_available_without_runtime_configuration():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["undo", "--help"])

    assert exc_info.value.code == 0
