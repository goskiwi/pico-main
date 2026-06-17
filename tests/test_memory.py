from pico.memory import LayeredMemory


def test_working_memory_tracks_summary_and_recent_files():
    memory = LayeredMemory()

    memory.set_goal("Investigate flaky tests")
    memory.update_working_state(
        current_subtask="Reproduce failure",
        next_action="Run focused tests",
        last_error="pytest timed out",
    )
    memory.remember_file("README.md")
    memory.remember_file("src/app.py")
    memory.remember_file("README.md")

    snapshot = memory.to_dict()

    assert snapshot["working"]["goal"] == "Investigate flaky tests"
    assert snapshot["working"]["current_subtask"] == "Reproduce failure"
    assert snapshot["working"]["next_action"] == "Run focused tests"
    assert snapshot["working"]["last_error"] == "pytest timed out"
    assert snapshot["working"]["recent_files"] == ["src/app.py", "README.md"]


def test_episodic_notes_append_and_retrieve_deterministically():
    memory = LayeredMemory()

    memory.append_note("Exact tag note", tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    memory.append_note("Keyword overlap note about memory", created_at="2026-04-07T10:01:00+00:00")
    memory.append_note("Newest unrelated note", created_at="2026-04-07T10:02:00+00:00")
    memory.append_note("Older unrelated note", created_at="2026-04-07T09:59:00+00:00")

    snapshot = memory.to_dict()
    assert [note["text"] for note in snapshot["episodic_notes"]] == [
        "Exact tag note",
        "Keyword overlap note about memory",
        "Newest unrelated note",
        "Older unrelated note",
    ]
    lines = [line for line in memory.retrieval_view("recall memory", limit=4).splitlines() if line.startswith("- ")]
    assert lines == [
        "- Exact tag note",
        "- Keyword overlap note about memory",
    ]


def test_file_summaries_use_canonical_paths_and_freshness(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    memory = LayeredMemory(workspace_root=tmp_path)

    memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    memory.remember_file("./sample.txt")
    snapshot = memory.to_dict()["file_summaries"]["sample.txt"]

    assert snapshot["summary"] == "sample.txt: alpha"
    assert snapshot["freshness"]

    rendered = memory.render_memory_text()
    assert "Working" in rendered
    assert "Recent Files" in rendered
    assert "File Summaries" in rendered
    assert "- sample.txt: sample.txt: alpha" in rendered
    file_path.write_text("beta\n", encoding="utf-8")
    assert "sample.txt: alpha" not in memory.render_memory_text()

    memory.invalidate_file_summary("sample.txt")

    assert "sample.txt" not in memory.to_dict()["file_summaries"]


def test_process_notes_keep_kind_and_latest_duplicate_wins():
    memory = LayeredMemory()

    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:00:00+00:00",
        kind="process",
    )
    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:01:00+00:00",
        kind="process",
    )

    notes = memory.to_dict()["episodic_notes"]

    assert len(notes) == 1
    assert notes[0]["kind"] == "process"
    assert notes[0]["created_at"] == "2026-04-07T10:01:00+00:00"


def test_durable_memory_index_and_type_notes_are_loaded_and_retrieved(tmp_path):
    memory_root = tmp_path / ".pico" / "memory"
    entries_dir = memory_root / "entries"
    entries_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project](entries/project.md): Project Memory\n"
        "  - summary: Project decisions, constraints, and dynamics.\n"
        "  - tags: project\n",
        encoding="utf-8",
    )
    (entries_dir / "project.md").write_text(
        "# Project Memory\n\n"
        "- type: project\n"
        "- summary: Project decisions, constraints, and dynamics.\n"
        "- tags: project\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n"
        "- Preserve local agent state under .pico/.\n",
        encoding="utf-8",
    )

    memory = LayeredMemory(workspace_root=tmp_path)

    snapshot = memory.to_dict()
    assert snapshot["durable_types"] == ["project"]

    lines = [line for line in memory.retrieval_view("constrained tools", limit=4).splitlines() if line.startswith("- ")]
    assert any("Use constrained tools instead of guessing." in line for line in lines)


def test_durable_memory_ignores_old_topic_layout(tmp_path):
    memory_root = tmp_path / ".pico" / "memory"
    topics_dir = memory_root / "topics"
    topics_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention\n",
        encoding="utf-8",
    )
    (topics_dir / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n",
        encoding="utf-8",
    )

    memory = LayeredMemory(workspace_root=tmp_path)

    assert memory.to_dict()["durable_types"] == []
    assert memory.retrieval_view("constrained tools", limit=4) == "Relevant memory:\n- none"


def test_durable_memory_index_is_limited_by_lines_and_bytes(tmp_path):
    memory_root = tmp_path / ".pico" / "memory"
    entries_dir = memory_root / "entries"
    entries_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        + "- [project](entries/project.md): Project Memory\n"
        + "  - summary: " + ("x" * 30_000) + "\n"
        + "  - tags: project\n",
        encoding="utf-8",
    )
    (entries_dir / "project.md").write_text(
        "# Project Memory\n\n"
        "- type: project\n"
        "- summary: Project decisions, constraints, and dynamics.\n"
        "- tags: project\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n",
        encoding="utf-8",
    )

    memory = LayeredMemory(workspace_root=tmp_path)

    assert memory.to_dict()["durable_types"] == ["project"]
    assert "Use constrained tools instead of guessing." in memory.retrieval_view("constrained tools", limit=4)
