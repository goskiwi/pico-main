
from scripts.run_real_system import TARGET_PATH, build_prompt, system_files


def test_real_system_fixture_is_multi_file_and_starts_with_the_pricing_bug():
    files = system_files()

    assert len(files) == 8
    assert "AGENTS-FOLLOWED" in files["AGENTS.md"]
    assert TARGET_PATH in files
    assert "item.unit_price for item in items" in files[TARGET_PATH]
    assert "item.quantity" not in files[TARGET_PATH]
    assert len([path for path in files if path.startswith("tests/")]) == 2


def test_real_system_prompt_describes_symptom_without_revealing_target_path():
    prompt = build_prompt()

    assert "quantity greater than one" in prompt
    assert "Use repository tools to locate" in prompt
    assert TARGET_PATH not in prompt
    assert "do not modify tests" in prompt
    assert "do not open AGENTS.md" in prompt


def test_real_child_prompt_requires_explicit_patch_integration():
    prompt = build_prompt(delegate=True)

    assert "exactly one implement Child" in prompt
    assert "integrate_child" in prompt
    assert "do not edit files directly" in prompt
