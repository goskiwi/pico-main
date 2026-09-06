import shlex
import subprocess
import sys

import pytest

from pico.command_runner import CommandResult, CommandRunner
from pico.evidence import verification_is_current
from pico.mutations import file_revision
from pico.verification import (
    RepositorySnapshotError,
    capture_repository_state,
    repository_state_changes,
    verify_workspace,
)


def _git(root, *args):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _committed_git_workspace(root):
    _git(root, "init", "--quiet")
    (root / ".gitignore").write_text(".pico/\ncache/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(
        root,
        "-c",
        "user.name=Pico Tests",
        "-c",
        "user.email=pico@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )


def test_runtime_verification_records_changed_path_drift(
    tmp_path,
):
    target = tmp_path / "subject.txt"
    target.write_text("before\n", encoding="utf-8")
    before = file_revision(target)

    class Runner:
        @staticmethod
        def run(*_args, **_kwargs):
            target.write_text("external\n", encoding="utf-8")
            return CommandResult(returncode=0, stdout="2 passed")

    result = verify_workspace(
        root=tmp_path,
        command="python -m pytest -q",
        command_runner=Runner(),
        timeout_seconds=60,
        redact_text=str,
        mutation_sequence_provider=lambda: 7,
        started_workspace_mutation_sequence=7,
        changed_paths=("subject.txt",),
    )

    assert result["status"] == "failed"
    assert "changed Runtime-tracked file contents: subject.txt" in result["output"]
    assert "freshness" not in result
    assert result["started_changed_path_states"] == {"subject.txt": before}
    assert result["finished_changed_path_states"] == {
        "subject.txt": file_revision(target)
    }
    assert not verification_is_current(
        result,
        7,
        {"subject.txt": file_revision(target)},
        "python -m pytest -q",
    )


def test_runtime_verification_rejects_extra_git_workspace_changes(tmp_path):
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.txt"],
        cwd=tmp_path,
        check=True,
    )

    class Runner:
        @staticmethod
        def run(*_args, **_kwargs):
            (tmp_path / "verifier-extra.txt").write_text(
                "unexpected\n",
                encoding="utf-8",
            )
            return CommandResult(returncode=0, stdout="tests passed")

    result = verify_workspace(
        root=tmp_path,
        command="verify",
        command_runner=Runner(),
        timeout_seconds=60,
        redact_text=str,
        mutation_sequence_provider=lambda: 0,
        started_workspace_mutation_sequence=0,
        changed_paths=(),
    )

    assert result["status"] == "failed"
    assert "changed additional workspace state" in result["output"]
    assert "verifier-extra.txt" in result["output"]
    assert (tmp_path / "verifier-extra.txt").is_file()


def test_repository_state_detects_dirty_file_content_changing_again(tmp_path):
    _committed_git_workspace(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("dirty-one\n", encoding="utf-8")
    before = capture_repository_state(tmp_path)

    target.write_text("dirty-two\n", encoding="utf-8")
    after = capture_repository_state(tmp_path)

    assert repository_state_changes(before, after) == ("tracked.txt",)


def test_repository_state_detects_index_only_change(tmp_path):
    _committed_git_workspace(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("dirty\n", encoding="utf-8")
    before = capture_repository_state(tmp_path)

    _git(tmp_path, "add", "tracked.txt")
    after = capture_repository_state(tmp_path)

    assert repository_state_changes(before, after) == ("tracked.txt",)


def test_repository_state_detects_head_change(tmp_path):
    _committed_git_workspace(tmp_path)
    before = capture_repository_state(tmp_path)

    _git(
        tmp_path,
        "-c",
        "user.name=Pico Tests",
        "-c",
        "user.email=pico@example.invalid",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "empty",
    )
    after = capture_repository_state(tmp_path)

    assert repository_state_changes(before, after) == ("<HEAD>",)


def test_repository_state_ignores_gitignored_artifacts(tmp_path):
    _committed_git_workspace(tmp_path)
    before = capture_repository_state(tmp_path)

    cache = tmp_path / "cache" / "result.txt"
    cache.parent.mkdir()
    cache.write_text("generated\n", encoding="utf-8")
    after = capture_repository_state(tmp_path)

    assert repository_state_changes(before, after) == ()


def test_repository_state_detects_existing_untracked_content_change(tmp_path):
    _committed_git_workspace(tmp_path)
    target = tmp_path / "notes.txt"
    target.write_text("draft-one\n", encoding="utf-8")
    before = capture_repository_state(tmp_path)

    target.write_text("draft-two\n", encoding="utf-8")
    after = capture_repository_state(tmp_path)

    assert repository_state_changes(before, after) == ("notes.txt",)


@pytest.mark.parametrize("nested", [False, True])
def test_snapshot_requires_all_entries_but_allows_exact_limit(tmp_path, monkeypatch, nested):
    monkeypatch.setattr("pico.verification.VERIFICATION_SNAPSHOT_MAX_ENTRIES", 2)
    directory = tmp_path / "sub" if nested else tmp_path
    directory.mkdir(exist_ok=True)
    for name in (["a"] if nested else ["a", "b"]):
        (directory / name).write_text("before")
    _kind, state = capture_repository_state(tmp_path)
    assert len(state) == 2
    (directory / "z").write_text("unobserved")
    with pytest.raises(RepositorySnapshotError, match="exceeded"):
        capture_repository_state(tmp_path)


def test_verifier_reports_unknown_effects_if_post_execution_scan_exceeds_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("pico.verification.VERIFICATION_SNAPSHOT_MAX_ENTRIES", 2)
    for name in ("a", "b"):
        (tmp_path / name).write_text("before")
    result = verify_workspace(
        root=tmp_path,
        command=shlex.join([sys.executable, "-B", "-c", "from pathlib import Path; Path('z').write_text('changed')"]),
        command_runner=CommandRunner(tmp_path), timeout_seconds=30, redact_text=str,
        mutation_sequence_provider=lambda: 0, started_workspace_mutation_sequence=0,
        changed_paths=(),
    )
    assert (tmp_path / "z").read_text() == "changed"
    assert result["status"] == "infrastructure_error"
    assert result["workspace_changes"] is None
    assert "exceeded" in result["output"]


def test_snapshot_does_not_silently_skip_unreadable_directories(tmp_path, monkeypatch):
    import os

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    scan = os.scandir

    def read_directory(path):
        if path == blocked:
            raise PermissionError("injected unreadable directory")
        return scan(path)

    monkeypatch.setattr("pico.verification.os.scandir", read_directory)
    with pytest.raises(RepositorySnapshotError, match="cannot observe directory"):
        capture_repository_state(tmp_path)


def test_git_snapshot_rejects_unreadable_untracked_content(tmp_path, monkeypatch):
    from pico.workspace import Workspace

    _committed_git_workspace(tmp_path)
    target = tmp_path / "unreadable.txt"
    target.write_text("before")
    observe = Workspace.path_state
    monkeypatch.setattr(Workspace, "path_state", staticmethod(
        lambda path: "unavailable" if path == target else observe(path),
    ))
    with pytest.raises(RepositorySnapshotError, match="cannot observe file contents"):
        capture_repository_state(tmp_path)
