from pico.session_store import SessionStore


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    first = {"id": "first", "history": [{"role": "user", "content": "hello"}]}
    second = {"id": "second", "history": [{"role": "assistant", "content": "hi"}]}

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == tmp_path / ".pico" / "sessions" / "first.json"
    assert second_path == tmp_path / ".pico" / "sessions" / "second.json"
    assert store.load("first") == first
    assert store.load("second") == second
    assert store.latest() == "second"
    assert not list((tmp_path / ".pico" / "sessions").glob("*.tmp"))


def test_session_store_latest_returns_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    assert store.latest() is None
