from pico.prompt_instructions import build_prompt_instructions


def test_stable_instructions_exclude_dynamic_context_and_tool_listing():
    instructions = build_prompt_instructions()

    assert "You are pico" in instructions.text
    assert "Workspace:" not in instructions.text
    assert "Tools:" not in instructions.text
    assert "memory_recall" not in instructions.text
    assert instructions.content_hash


def test_project_memory_rules_are_explicitly_enabled():
    instructions = build_prompt_instructions(enable_project_memory=True)

    assert "memory_recall" in instructions.text
    assert "memory_store" in instructions.text
