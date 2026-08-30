import subprocess

from pico import WorkspaceContext
from pico.workspace_tracker import WorkspaceTracker


def test_workspace_tracker_has_no_full_workspace_snapshot_api(tmp_path):
    tracker = WorkspaceTracker(WorkspaceContext.build(tmp_path))

    assert not hasattr(tracker, "capture_snapshot")
    assert not hasattr(tracker, "content_fingerprint")
    assert not hasattr(tracker, "_scan_snapshot")


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


def test_workspace_context_does_not_preload_symlinked_document_names(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("OUTSIDE-SECRET\n", encoding="utf-8")
    (tmp_path / "README.md").symlink_to(outside)

    context = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)

    assert "OUTSIDE-SECRET" not in context.text()
    assert context.document_names == ()
    assert context.repository_conventions == {}


def test_model_workspace_panel_never_exposes_host_absolute_paths(tmp_path):
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)
    context = WorkspaceContext.build(nested, repo_root_override=tmp_path)

    text = context.text()

    assert str(tmp_path) not in text
    assert "- cwd: src/package" in text
    assert "repo_root" not in text
    assert "shell_cwd" not in text
    assert not hasattr(context, "default_branch")
    assert not hasattr(context, "recent_commits")
    assert set(context.state()) == {
        "cwd",
        "repo_root",
        "branch",
        "git_status",
        "document_names",
        "repository_conventions",
    }
