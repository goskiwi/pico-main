from datetime import datetime, timezone

import pytest

from pico.features.memory import SessionWorkingMemory
from pico.project_memory import ProjectMemoryStore


def _store(store, filename="reference_test_command.md", **overrides):
    values = {
        "action": "create",
        "filename": filename,
        "name": "Project test command",
        "description": "How this project runs focused tests.",
        "memory_type": filename.split("_", 1)[0],
        "content": "Run `python3 -m pytest -q`.",
        "origin": "explicit",
        "source_session_id": "session",
        "source_run_id": "run",
        "source_entry_ids": ("evidence-1",),
    }
    values.update(overrides)
    return store.store(**values)


def test_working_memory_is_revision_bound_and_invalidates_stale_observation(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("alpha\n", encoding="utf-8")
    memory = SessionWorkingMemory(workspace_root=tmp_path)
    memory.set_goal("Inspect sample").remember_file("sample.txt")
    memory.set_file_observation(
        "sample.txt",
        "sample contains alpha",
        source_session_id="session",
        source_run_id="run",
        source_tool_call_id="call",
        source_artifact_id="artifact",
    )
    rendered, metadata = memory.render_recall("sample alpha")
    assert "sample contains alpha" in rendered
    assert metadata["working_entry_ids"]
    path.write_text("beta\n", encoding="utf-8")
    rendered, metadata = memory.render_recall("sample alpha")
    assert "sample contains alpha" not in rendered
    assert metadata["working_entry_ids"] == []


def test_markdown_card_is_source_of_truth_and_index_is_generated(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory", tmp_path)
    card, action = _store(store)
    assert action == "created"
    assert store.recall(card.filename) == card
    assert (tmp_path / ".pico/memory/cards/reference_test_command.md").is_file()
    assert "reference_test_command.md" in store.index_text()


def test_markdown_memory_uses_filename_identity_and_explicit_wins(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory", tmp_path)
    explicit, _ = _store(store)
    with pytest.raises(ValueError, match="use update"):
        _store(store)
    kept, action = _store(
        store, action="update", origin="automatic", content="untrusted replacement"
    )
    assert action == "kept_explicit"
    assert kept == explicit
    updated, action = _store(store, action="update", content="Run focused pytest.")
    assert action == "updated"
    assert updated.version == 2
    unchanged, action = _store(store, action="update", content="Run focused pytest.")
    assert action == "unchanged"
    assert unchanged == updated
    assert store.forget(updated.filename) == updated
    assert store.recall(updated.filename) is None


def test_project_memory_selector_only_accepts_manifest_filenames(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory", tmp_path)
    _store(store)
    cards = store.selected_cards(["reference_test_command.md"])
    assert cards[0].content == "Run `python3 -m pytest -q`."
    rendered = store.render_selected(cards)
    assert "not workspace paths" in rendered
    assert "origin: explicit" in rendered
    with pytest.raises(ValueError, match="unavailable filename"):
        store.selected_cards(["reference_missing.md"])


def test_legacy_jsonl_memory_is_deleted_not_migrated(tmp_path):
    root = tmp_path / ".pico/memory"
    topics = root / "topics"
    topics.mkdir(parents=True)
    (root / "records.jsonl").write_text('{"legacy": true}\n', encoding="utf-8")
    (root / "index.md").write_text("legacy\n", encoding="utf-8")
    (topics / "old.md").write_text("legacy\n", encoding="utf-8")
    store = ProjectMemoryStore(root, tmp_path)
    assert store.count() == 0
    assert not (root / "records.jsonl").exists()
    assert not (root / "index.md").exists()
    assert not topics.exists()


def test_stale_selected_card_warns_before_use(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory", tmp_path)
    card, _ = _store(store)
    path = tmp_path / ".pico/memory/cards" / card.filename
    text = path.read_text(encoding="utf-8").replace(
        f'updated_at: "{card.updated_at}"',
        'updated_at: "2020-01-01T00:00:00+00:00"',
    )
    path.write_text(text, encoding="utf-8")
    rendered = store.render_selected([store.recall(card.filename)])
    assert "WARNING: saved" in rendered
    assert datetime.now(timezone.utc).year >= 2026
