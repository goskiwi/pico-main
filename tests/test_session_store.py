import json

import pytest

from pico.runtime_session import RuntimeSession
from pico.session_store import SESSION_SCHEMA_VERSION, SessionStore


def session(session_id, active_run_id=""):
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "id": session_id,
        "created_at": "2026-04-07T10:00:00+00:00",
        "workspace_root": "/workspace",
        "active_run_id": active_run_id,
    }


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    first = session("session_001", "run_first")
    second = session("session_002", "run_second")

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert json.loads(first_path.read_text(encoding="utf-8"))["id"] == "session_001"
    assert store.load("session_002") == second
    assert store.latest() == second_path.stem


def test_session_store_latest_is_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    assert store.latest() is None


def test_session_store_rejects_old_schema_and_unsafe_id(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    with pytest.raises(ValueError, match="schema"):
        store.save({"id": "old"})
    with pytest.raises(ValueError, match="session id"):
        store.path("../escape")


def test_runtime_session_commits_copy_only_after_persistence(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    runtime_session = RuntimeSession(store, tmp_path)
    runtime_session.save()
    before = runtime_session.data.copy()
    assert "memory" not in before

    def fail_save(_candidate):
        raise OSError("session persistence failed")

    monkeypatch.setattr(store, "save", fail_save)

    with pytest.raises(OSError, match="session persistence failed"):
        runtime_session.set_active_run("run_new")
    assert runtime_session.data == before

    with pytest.raises(OSError, match="session persistence failed"):
        runtime_session.reset()
    assert runtime_session.data == before
