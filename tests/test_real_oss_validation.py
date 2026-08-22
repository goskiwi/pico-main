import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.materialize_real_oss import load_manifest, tree_digest
from scripts.real_case_support import (
    changed_paths,
    file_snapshot,
    matches,
    require_clean_runtime,
)


def test_real_case_manifest_points_to_three_frozen_upstream_repositories():
    manifest = Path("validation/real_oss_suite.json")
    payload = load_manifest(manifest)

    assert [task["id"] for task in payload["tasks"]] == [
        "click_empty_bytes_echo",
        "packaging_non_string_version",
        "urllib3_port_zero",
    ]
    assert all(len(task["source_commit"]) == 40 for task in payload["tasks"])
    materialization = json.loads(
        Path("artifacts/real-oss-fixtures/.real_oss_suite.materialization.json")
        .read_text()
    )
    expected = {
        item["task_id"]: item["tree_digest"]
        for item in materialization["tasks"]
    }
    assert set(expected) == {task["id"] for task in payload["tasks"]}
    assert all(
        tree_digest(task["fixture_repo"]) == expected[task["id"]]
        for task in payload["tasks"]
    )


def test_file_snapshot_and_scope_ignore_runtime_artifacts(tmp_path):
    (tmp_path / "src" / "package").mkdir(parents=True)
    target = tmp_path / "src" / "package" / "code.py"
    target.write_text("before\n")
    (tmp_path / ".pico" / "runs").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "event.json").write_text("{}")
    before = file_snapshot(tmp_path)
    target.write_text("after\n")
    after = file_snapshot(tmp_path)

    assert changed_paths(before, after) == ["src/package/code.py"]
    assert matches("src/package/code.py", ["src/**/*.py"])
    assert matches("src/code.py", ["src/**/*.py"])


def test_real_case_publication_rejects_dirty_runtime():
    with pytest.raises(RuntimeError, match="requires a clean worktree"):
        require_clean_runtime({"working_tree_dirty": True})


def test_reference_patches_apply_to_current_fixtures(tmp_path):
    payload = load_manifest("validation/real_oss_suite.json")
    for task in payload["tasks"]:
        workspace = tmp_path / task["id"]
        shutil.copytree(task["fixture_repo"], workspace)
        result = subprocess.run(
            [
                "git", "apply", "--check", "--unidiff-zero",
                str(Path(task["reference_patch"]).resolve()),
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
