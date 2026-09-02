import subprocess

import pytest

from applications.coding import CodingWorkflow
from pico import FakeModelClient, ModelAction, PicoConfig
from pico.mutations import file_revision


def git(root, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def init_repository(root):
    git(root, "init")
    git(root, "config", "user.name", "Pico Test")
    git(root, "config", "user.email", "pico@example.test")
    (root / "subject.txt").write_text("alpha\n", encoding="utf-8")
    (root / "user.txt").write_text("clean\n", encoding="utf-8")
    git(root, "add", "subject.txt", "user.txt")
    git(root, "commit", "-m", "initial")
    return git(root, "rev-parse", "HEAD")


def edit_client(root, *, old_text="alpha\n", new_text="agent\n"):
    revision = file_revision(root / "subject.txt")
    return FakeModelClient(
        [
            ModelAction.tool(
                "read_file",
                {"path": "subject.txt", "start_line": 1, "end_line": 20},
                call_id="call_read",
            ),
            ModelAction.tool(
                "edit_file",
                {
                    "path": "subject.txt",
                    "old_text": old_text,
                    "new_text": new_text,
                    "expected_revision": revision,
                },
                call_id="call_edit",
            ),
            ModelAction.final("Implemented the requested change."),
        ]
    )


def workflow(client):
    return CodingWorkflow(
        client,
        config=PicoConfig(
            mode="auto",
            verification_command="grep -q '^agent$' subject.txt",
        ),
    )


def test_coding_workflow_requires_runtime_verification():
    client = FakeModelClient([])

    with pytest.raises(ValueError, match="requires verification_command"):
        CodingWorkflow(
            client,
            config=PicoConfig(mode="auto", verification_command=""),
        )

    with pytest.raises(ValueError, match="requires Auto mode"):
        CodingWorkflow(
            client,
            config=PicoConfig(mode="code", verification_command="verify"),
        )


def test_coding_workflow_commits_only_pico_paths_and_preserves_user_index(tmp_path):
    initial = init_repository(tmp_path)
    (tmp_path / "user.txt").write_text("user staged\n", encoding="utf-8")
    git(tmp_path, "add", "user.txt")

    result = workflow(edit_client(tmp_path)).run(
        tmp_path,
        "Replace alpha with agent",
    )

    assert result.outcome.status == "completed"
    assert result.delivery_status == "committed"
    assert result.changed_paths == ("subject.txt",)
    assert result.commit_sha == git(tmp_path, "rev-parse", "HEAD")
    assert result.commit_sha != initial
    assert git(tmp_path, "show", "HEAD:subject.txt") == "agent"
    assert git(
        tmp_path,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "HEAD",
    ) == "subject.txt"
    assert git(tmp_path, "diff", "--cached", "--name-only") == "user.txt"
    assert git(tmp_path, "log", "-1", "--pretty=%s") == (
        "pico: Replace alpha with agent"
    )
