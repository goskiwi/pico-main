from pico.skills import compute_active_tools, load_skills


def test_strict_skill_can_only_attenuate_the_runtime_tool_set(tmp_path):
    skill_path = tmp_path / ".pico" / "skills" / "audit" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: audit
tools: [read_file, search, nonexistent_tool]
allowed_tools_strict: true
---
Read-only audit.
""",
        encoding="utf-8",
    )

    skills = load_skills(tmp_path)
    active_tools, strict = compute_active_tools(
        skills,
        {"read_file", "search", "patch_file"},
    )

    assert strict is True
    assert active_tools == frozenset({"read_file", "search"})
