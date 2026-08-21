from datetime import datetime, timezone

import pytest

from pico.context_manager import Tokenizer
from pico.features.memory import WorkingState, normalize_working_update
from pico.project_memory import ProjectMemoryStore


def _store(store, filename="reference_test_command.md", **overrides):
    values = {
        "action": "create",
        "filename": filename,
        "name": "Project test command",
        "description": "How this project runs focused tests.",
        "memory_type": filename.split("_", 1)[0],
        "content": "Run `python3 -m pytest -q`.",
        "source_session_id": "session",
        "source_run_id": "run",
        "source_event_ids": ("evidence-1",),
    }
    values.update(overrides)
    return store.store(**values)


def _render_recalled(store, cards):
    tokenizer = Tokenizer()
    rendered, _ = store.render_recalled_with_budget(
        cards,
        max_tokens=100_000,
        token_counter=tokenizer.count,
    )
    return rendered


def test_working_state_tracks_goal_constraints_decisions_and_next_steps():
    state = WorkingState(goal="Fix the timeout")
    state.apply_update(
        {
            "add_constraints": ["Keep Python 3.10 compatibility"],
            "add_decisions": ["The race is in token refresh"],
            "add_next_steps": ["Add a concurrent refresh test"],
        }
    )

    assert state.to_dict() == {
        "schema_version": "run-working-state-v1",
        "goal": "Fix the timeout",
        "constraints": ["Keep Python 3.10 compatibility"],
        "decisions": ["The race is in token refresh"],
        "next_steps": ["Add a concurrent refresh test"],
    }
    assert "The race is in token refresh" in state.render_panel()


def test_working_state_updates_are_incremental_and_idempotent():
    state = WorkingState(
        goal="Fix the timeout",
        constraints=("Do not change the schema",),
        next_steps=("Inspect token refresh",),
    )
    update = normalize_working_update(
        {
            "add_constraints": ["Do not change the schema"],
            "remove_next_steps": ["Inspect token refresh"],
            "add_next_steps": ["Add a regression test"],
        }
    )

    state.apply_update(update)

    assert state.constraints == ("Do not change the schema",)
    assert state.next_steps == ("Add a regression test",)


def test_markdown_card_is_source_of_truth_and_index_is_generated(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    card, action = _store(store)
    assert action == "created"
    assert store.recall(card.filename) == card
    assert (tmp_path / ".pico/memory/cards/reference_test_command.md").is_file()
    assert "reference_test_command.md" in store.index_text()


def test_markdown_memory_uses_filename_identity(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    _store(store)
    with pytest.raises(ValueError, match="use update"):
        _store(store)
    updated, action = _store(store, action="update", content="Run focused pytest.")
    assert action == "updated"
    assert updated.version == 2
    unchanged, action = _store(store, action="update", content="Run focused pytest.")
    assert action == "unchanged"
    assert unchanged == updated
    assert store.forget(updated.filename) == updated
    assert store.recall(updated.filename) is None


def test_project_memory_recall_only_accepts_available_filenames(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    _store(store)
    cards = store.recall_cards(["reference_test_command.md"])
    assert cards[0].content == "Run `python3 -m pytest -q`."
    rendered = _render_recalled(store, cards)
    assert "not workspace paths" in rendered
    assert "Project test command" in rendered
    with pytest.raises(ValueError, match="unavailable filename"):
        store.recall_cards(["reference_missing.md"])


def test_recalled_memories_are_packed_as_complete_cards(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    first, _ = _store(store, filename="reference_first.md", content="first complete card")
    second, _ = _store(store, filename="reference_second.md", content="second complete card")
    tokenizer = Tokenizer()
    one_card_budget = tokenizer.count(_render_recalled(store, [first]))

    rendered, included = store.render_recalled_with_budget(
        [first, second],
        max_tokens=one_card_budget,
        token_counter=tokenizer.count,
    )

    assert [card.filename for card in included] == ["reference_first.md"]
    assert "first complete card" in rendered
    assert "second complete card" not in rendered


def test_legacy_memory_is_ignored_and_preserved(tmp_path):
    root = tmp_path / ".pico/memory"
    pico_root = root.parent
    topics = root / "topics"
    vector_root = pico_root / "memory-vector-index"
    topics.mkdir(parents=True)
    vector_root.mkdir(parents=True)
    sqlite = pico_root / "project-memory.sqlite3"
    sqlite.write_bytes(b"legacy sqlite")
    (vector_root / "index.bin").write_bytes(b"legacy vector")
    (root / "records.jsonl").write_text('{"legacy": true}\n', encoding="utf-8")
    (root / "index.md").write_text("legacy\n", encoding="utf-8")
    (topics / "old.md").write_text("legacy\n", encoding="utf-8")

    store = ProjectMemoryStore(root)

    assert store.count() == 0
    assert sqlite.read_bytes() == b"legacy sqlite"
    assert (vector_root / "index.bin").read_bytes() == b"legacy vector"
    assert (root / "records.jsonl").read_text(encoding="utf-8") == '{"legacy": true}\n'
    assert (root / "index.md").read_text(encoding="utf-8") == "legacy\n"
    assert (topics / "old.md").read_text(encoding="utf-8") == "legacy\n"
    assert store.index_path.is_file()


def test_stale_recalled_card_warns_before_use(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    card, _ = _store(store)
    path = tmp_path / ".pico/memory/cards" / card.filename
    text = path.read_text(encoding="utf-8").replace(
        f'updated_at: "{card.updated_at}"',
        'updated_at: "2020-01-01T00:00:00+00:00"',
    )
    path.write_text(text, encoding="utf-8")
    rendered = _render_recalled(store, [store.recall(card.filename)])
    assert "WARNING: saved" in rendered
    assert datetime.now(timezone.utc).year >= 2026
