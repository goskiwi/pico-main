import subprocess

from pico.command_runner import CommandResult
from pico.evidence import verification_is_current
from pico.mutations import file_revision
from pico.verification import (
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
