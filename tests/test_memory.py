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
        "source_run_id": "run",
        "source_tool_call_id": "call",
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


def test_working_state_tracks_constraints_decisions_and_next_steps():
    state = WorkingState()
    state.apply_update(
        {
            "add_constraints": ["Keep Python 3.10 compatibility"],
            "add_decisions": ["The race is in token refresh"],
            "add_next_steps": ["Add a concurrent refresh test"],
        }
    )

    assert state.to_dict() == {
        "schema_version": "run-working-state-v2",
        "constraints": ["Keep Python 3.10 compatibility"],
        "decisions": ["The race is in token refresh"],
        "next_steps": ["Add a concurrent refresh test"],
    }
    assert "The race is in token refresh" in state.render_panel()


def test_working_state_updates_are_incremental_and_idempotent():
    state = WorkingState(
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


def test_catalog_refresh_after_manual_card_edit_is_explicit(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    card, _ = _store(store)
    path = store.cards_root / card.filename
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Project test command",
            "Manually edited command",
        ),
        encoding="utf-8",
    )

    stale_catalog = store.index_text()
    store.refresh_index()
    catalog = store.index_text()

    assert "Project test command" in stale_catalog
    assert "Manually edited command" not in stale_catalog
    assert "Manually edited command" in catalog
    assert "Project test command" not in catalog


def test_project_memory_rejects_symlinked_cards_and_index(tmp_path):
    root = tmp_path / "memory"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "cards").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="cards path must not be a symlink"):
        ProjectMemoryStore(root)

    (root / "cards").unlink()
    (root / "cards").mkdir()
    outside_index = outside / "MEMORY.md"
    outside_index.write_text("outside\n", encoding="utf-8")
    (root / "MEMORY.md").symlink_to(outside_index)

    with pytest.raises(ValueError, match="index must not be a symlink"):
        ProjectMemoryStore(root)


def test_memory_frontmatter_field_order_is_not_semantic(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    card, _ = _store(store)
    path = store.cards_root / card.filename
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2], lines[3] = lines[3], lines[2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert store.recall(card.filename).content == card.content


def test_project_memory_rejects_v4_cards_without_migration(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    card, _ = _store(store)
    path = store.cards_root / card.filename
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "pico-markdown-project-memory-v5",
            "pico-markdown-project-memory-v4",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported memory schema"):
        store.recall(card.filename)


def test_markdown_memory_uses_filename_identity(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    _store(store)
    with pytest.raises(ValueError, match="use update"):
        _store(store)
    updated, action = _store(store, action="update", content="Run focused pytest.")
    assert action == "updated"
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

    assert store.list_cards() == []
    assert sqlite.read_bytes() == b"legacy sqlite"
    assert (vector_root / "index.bin").read_bytes() == b"legacy vector"
    assert (root / "records.jsonl").read_text(encoding="utf-8") == '{"legacy": true}\n'
    assert (root / "index.md").read_text(encoding="utf-8") == "legacy\n"
    assert (topics / "old.md").read_text(encoding="utf-8") == "legacy\n"
    assert store.index_path.is_file()


def test_recalled_card_shows_updated_at_without_arbitrary_age_warning(tmp_path):
    store = ProjectMemoryStore(tmp_path / ".pico/memory")
    card, _ = _store(store)
    path = tmp_path / ".pico/memory/cards" / card.filename
    text = path.read_text(encoding="utf-8").replace(
        f'updated_at: "{card.updated_at}"',
        'updated_at: "2020-01-01T00:00:00+00:00"',
    )
    path.write_text(text, encoding="utf-8")
    rendered = _render_recalled(store, [store.recall(card.filename)])
    assert "updated_at: 2020-01-01T00:00:00+00:00" in rendered
    assert "WARNING: saved" not in rendered
    assert datetime.now(timezone.utc).year >= 2026
