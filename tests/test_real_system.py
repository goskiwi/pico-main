import json
from pathlib import Path

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


def test_published_real_cli_system_artifact_passes_every_boundary():
    artifact = json.loads(Path("artifacts/real-system.json").read_text())

    assert artifact["runtime"]["working_tree_dirty"] is False
    assert artifact["runtime"]["commit_sha"]
    assert artifact["cli"]["exit_code"] == 0
    assert artifact["changed_paths"] == [TARGET_PATH]
    assert artifact["analysis"]["model_request_count"] <= 10
    assert artifact["analysis"]["executed_tool_count"] <= 12
    assert artifact["verification"]["initial"]["ok"] is False
    assert artifact["verification"]["visible"]["ok"] is True
    assert artifact["verification"]["hidden"]["ok"] is True
    assert artifact["checks"]["auto_verifier_selected"] is True
    assert artifact["checks"][
        "repository_instruction_followed_without_file_read"
    ] is True
    assert artifact["checks"]["target_located_without_prompt_hint"] is True
    assert "Status: completed" in artifact["cli"]["stdout"]
    assert "Verification: passed" in artifact["cli"]["stdout"]
    assert "AGENTS-FOLLOWED" in artifact["cli"]["stdout"]
    assert artifact["passed"] is True
    assert all(artifact["checks"].values())


def test_published_real_child_artifact_uses_the_new_patch_receipt():
    artifact = json.loads(Path("artifacts/real-child.json").read_text())

    assert artifact["passed"] is True
    assert all(artifact["checks"].values())
    assert artifact["cli"]["exit_code"] == 0
    receipt, = artifact["analysis"]["child_receipts"]
    assert set(receipt) == {
        "child_id", "child_run_id", "role", "status", "result", "patch"
    }
    assert set(receipt["patch"]) == {
        "base_sha", "changed_paths", "sha256", "integrated"
    }
    assert receipt["patch"]["changed_paths"] == [TARGET_PATH]
    assert artifact["checks"]["parent_did_not_edit_directly"] is True
