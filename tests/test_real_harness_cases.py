

from scripts.run_real_harness_cases import ASK_TOOLS, build_prompt, prepare_workspace


def test_controlled_workspace_is_reproducible(tmp_path):
    workspace = prepare_workspace(
        tmp_path / "harness",
        {"README.md": "hello\n", "src/example.py": "VALUE = 1\n"},
    )

    assert (workspace / "README.md").read_text() == "hello\n"
    assert (workspace / "src/example.py").read_text() == "VALUE = 1\n"
    assert (workspace / ".git").is_dir()


def test_real_prompts_make_expected_model_behavior_observable():
    assert "read the file first" in build_prompt("ask")
    assert "If approval is denied, do not retry" in build_prompt("approval")
    assert "read the file again and retry" in build_prompt("revision")
    assert "do not repeat the interrupted edit blindly" in build_prompt("resume")


def test_ask_surface_expectation_contains_no_mutation_tools():
    assert ASK_TOOLS == {
        "list_files",
        "read_artifact",
        "read_file",
        "search",
        "submit_final",
        "update_working_state",
    }
    assert {"write_file", "edit_file", "run_command"}.isdisjoint(ASK_TOOLS)
