import json

import pytest

from pico.persistence import atomic_write_json


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
