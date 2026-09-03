import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    "path",
    [
        "artifacts/real-ask.json",
        "artifacts/real-code-approval.json",
        "artifacts/real-revision-repair.json",
        "artifacts/real-resume.json",
    ],
)
def test_published_real_harness_artifacts_pass_all_checks(path):
    artifact = json.loads(Path(path).read_text())

    assert artifact["model"]
    assert artifact["runtime"]["commit_sha"]
    assert artifact["runtime"]["working_tree_dirty"] is False
    assert artifact["passed"] is True
    assert all(artifact["checks"].values())


def test_published_real_harness_artifacts_cover_distinct_runtime_paths():
    ask = json.loads(Path("artifacts/real-ask.json").read_text())
    approval = json.loads(Path("artifacts/real-code-approval.json").read_text())
    revision = json.loads(Path("artifacts/real-revision-repair.json").read_text())
    resume = json.loads(Path("artifacts/real-resume.json").read_text())

    assert ask["changed_paths"] == []
    assert approval["changed_paths"] == []
    assert revision["changed_paths"] == ["subject.txt"]
    assert resume["changed_paths"] == ["recovery.txt"]
    assert revision["analysis"]["model_request_count"] <= 8
    assert resume["analysis"]["model_request_count"] <= 8
