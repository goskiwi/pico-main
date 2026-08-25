import json

import pytest

from pico.persistence import atomic_replace_bytes, atomic_write_json


def test_atomic_write_json_replaces_complete_snapshot(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}\n', encoding="utf-8")

    assert atomic_write_json(path, {"z": 1, "a": 2}) == path

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2, "z": 1}
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert list(tmp_path.glob("state.json.*.tmp")) == []


def test_atomic_write_json_failure_preserves_previous_snapshot(tmp_path):
    path = tmp_path / "state.json"
    original = '{"stable": true}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(TypeError):
        atomic_write_json(path, {"invalid": object()})

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("state.json.*.tmp")) == []


def test_atomic_write_json_fsyncs_temporary_file(tmp_path, monkeypatch):
    calls = []

    def record_fsync(descriptor):
        calls.append(descriptor)

    monkeypatch.setattr("pico.persistence.os.fsync", record_fsync)

    atomic_write_json(tmp_path / "nested" / "state.json", {"value": 1})

    assert len(calls) == 1


def test_atomic_replace_runs_commit_guard_after_staging_and_cleans_up_on_failure(
    tmp_path,
):
    path = tmp_path / "state.txt"
    path.write_text("stable\n", encoding="utf-8")
    observed_temporary_files = []

    def reject_commit():
        observed_temporary_files.extend(tmp_path.glob("state.txt.*.tmp"))
        raise RuntimeError("commit condition changed")

    with pytest.raises(RuntimeError, match="commit condition changed"):
        atomic_replace_bytes(
            path,
            b"replacement\n",
            commit_guard=reject_commit,
        )

    assert len(observed_temporary_files) == 1
    assert path.read_text(encoding="utf-8") == "stable\n"
    assert list(tmp_path.glob("state.txt.*.tmp")) == []
