import hashlib

from pico.prompt_instructions import build_prompt_instructions


def test_stable_instructions_have_only_the_five_runtime_sections():
    instructions = build_prompt_instructions()

    headings = [
        line.removesuffix(":")
        for line in instructions.text.splitlines()
        if line.endswith(":") and not line.startswith("-")
    ]
    assert headings == ["Role", "Execution", "Tools", "Working state", "Completion"]
    assert instructions.text.count("You are pico") == 1


def test_stable_instructions_exclude_dynamic_context_and_specific_tools():
    instructions = build_prompt_instructions()

    for dynamic_text in (
        "Workspace:",
        "Task contract",
        "Current user request",
        "RepoMap",
        "AGENTS.md",
        "write_file",
        "edit_file",
        "submit_final",
    ):
        assert dynamic_text not in instructions.text


def test_stable_instructions_are_deterministic_and_content_addressed():
    first = build_prompt_instructions()
    second = build_prompt_instructions()

    assert second == first
    assert first.content_hash == hashlib.sha256(first.text.encode("utf-8")).hexdigest()
