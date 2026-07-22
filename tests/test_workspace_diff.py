from pathlib import Path

import pytest

import pico.workspace_diff as workspace_diff
from tests.helpers import build_agent


def test_workspace_snapshot_reuses_hashes_for_unchanged_files(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [])
    (tmp_path / "notes.txt").write_text("alpha\n", encoding="utf-8")

    original_read_bytes = Path.read_bytes
    read_counts = {}

    def counting_read_bytes(path):
        relative_path = path.relative_to(tmp_path).as_posix()
        read_counts[relative_path] = read_counts.get(relative_path, 0) + 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    first = workspace_diff.capture_workspace_snapshot(agent)
    second = workspace_diff.capture_workspace_snapshot(agent)

    assert first == second
    assert read_counts["README.md"] == 1
    assert read_counts["notes.txt"] == 1

    (tmp_path / "notes.txt").write_text("alpha changed\n", encoding="utf-8")

    third = workspace_diff.capture_workspace_snapshot(agent)

    assert third["README.md"] == first["README.md"]
    assert third["notes.txt"] != first["notes.txt"]
    assert read_counts["README.md"] == 1
    assert read_counts["notes.txt"] == 2


def test_path_snapshot_reuses_hashes_for_unchanged_target(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [])

    original_read_bytes = Path.read_bytes
    read_count = 0

    def counting_read_bytes(path):
        nonlocal read_count
        read_count += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    first = workspace_diff.capture_path_snapshot(agent, ["README.md"])
    second = workspace_diff.capture_path_snapshot(agent, ["README.md"])

    assert first == second
    assert read_count == 1


def test_path_snapshot_does_not_hide_invalid_paths(tmp_path):
    agent = build_agent(tmp_path, [])

    with pytest.raises(ValueError, match="escapes workspace"):
        workspace_diff.capture_path_snapshot(agent, ["../outside.txt"])


def test_shell_snapshot_detects_changes_to_already_dirty_file(tmp_path):
    agent = build_agent(tmp_path, [])
    target = tmp_path / "dirty.txt"
    target.write_text("before\n", encoding="utf-8")
    tool = agent.tools["run_shell"]

    before, mode = workspace_diff.before_workspace_snapshot(
        agent, "run_shell", {"command": "pytest -q"}, tool
    )
    target.write_text("after changed\n", encoding="utf-8")
    after = workspace_diff.after_workspace_snapshot(
        agent, "run_shell", {"command": "pytest -q"}, tool, mode, before
    )

    changed, summaries = workspace_diff.diff_workspace_snapshots(before, after)
    assert mode == "full"
    assert changed == ["dirty.txt"]
    assert summaries == ["modified:dirty.txt"]
