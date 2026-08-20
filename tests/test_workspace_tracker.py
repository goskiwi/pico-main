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
