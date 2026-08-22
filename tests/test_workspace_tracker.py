import subprocess

from pico import WorkspaceContext
from pico.workspace_tracker import WorkspaceTracker


def test_forced_content_fingerprint_detects_external_edit(tmp_path):
    target = tmp_path / "subject.txt"
    target.write_text("alpha\n", encoding="utf-8")
    tracker = WorkspaceTracker(WorkspaceContext.build(tmp_path))
    before = tracker.content_fingerprint()

    target.write_text("beta\n", encoding="utf-8")

    assert tracker.content_fingerprint() == before
    assert tracker.content_fingerprint(force=True) != before


def test_workspace_snapshot_does_not_expose_internal_cache(tmp_path):
    (tmp_path / "subject.txt").write_text("alpha\n", encoding="utf-8")
    tracker = WorkspaceTracker(WorkspaceContext.build(tmp_path))

    snapshot = tracker.capture_snapshot(force=True)
    snapshot.clear()

    assert "subject.txt" in tracker.capture_snapshot()


def test_workspace_snapshot_skips_file_symlinks(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    tracker = WorkspaceTracker(WorkspaceContext.build(tmp_path))

    snapshot = tracker.capture_snapshot(force=True)

    assert "linked.txt" not in snapshot


def test_workspace_git_status_excludes_pico_from_nested_cwd(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    nested = tmp_path / "src"
    nested.mkdir()
    (tmp_path / ".pico" / "sessions").mkdir(parents=True)
    (tmp_path / ".pico" / "sessions" / "state.json").write_text("runtime\n")
    (tmp_path / "user.txt").write_text("user\n", encoding="utf-8")

    context = WorkspaceContext.build(nested)

    assert ".pico" not in context.git_status
    assert "user.txt" in context.git_status


def test_workspace_context_does_not_preload_symlinked_project_docs(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("OUTSIDE-SECRET\n", encoding="utf-8")
    (tmp_path / "README.md").symlink_to(outside)

    context = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)

    assert "OUTSIDE-SECRET" not in context.text()
    assert context.project_docs == {}
