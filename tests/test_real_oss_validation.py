import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path("scripts/run_real_oss_validation.py")
SPEC = importlib.util.spec_from_file_location("run_real_oss_validation", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_real_oss_manifest_is_strict_and_points_to_frozen_upstream():
    task = MODULE.load_task(Path("validation/click_real_oss.json"))

    assert task["id"] == "click_empty_bytes_echo"
    assert task["source_repository"] == "https://github.com/pallets/click.git"
    assert len(task["source_commit"]) == 40
    assert task["forbidden_change_globs"] == ["tests/**", ".pico_hidden_verifier/**"]


def test_file_snapshot_and_scope_ignore_runtime_artifacts(tmp_path):
    (tmp_path / "src" / "package").mkdir(parents=True)
    target = tmp_path / "src" / "package" / "code.py"
    target.write_text("before\n")
    (tmp_path / ".pico" / "runs").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "event.json").write_text("{}")
    before = MODULE.file_snapshot(tmp_path)
    target.write_text("after\n")
    after = MODULE.file_snapshot(tmp_path)

    assert MODULE.changed_paths(before, after) == ["src/package/code.py"]
    assert MODULE.matches("src/package/code.py", ["src/**/*.py"])


def test_real_oss_publication_rejects_dirty_runtime():
    with pytest.raises(RuntimeError, match="requires a clean worktree"):
        MODULE.require_clean_runtime({"working_tree_dirty": True})
