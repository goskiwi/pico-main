import json
import shutil
from pathlib import Path

import pytest

from scripts import official_test_support as MODULE


def test_official_public_manifest_is_bound_and_explicit():
    manifest = MODULE.load_manifest()
    assert len(manifest["tasks"]) == 3
    assert {
        task["id"]: task["pre_fix_expected"] for task in manifest["tasks"]
    } == {
        "click_empty_bytes_echo": "fail",
        "packaging_non_string_version": "fail",
        "urllib3_port_zero": "fail",
    }
    assert all(len(task["source_commit"]) == 40 for task in manifest["tasks"])
    assert all(len(task["official_fix_commit"]) == 40 for task in manifest["tasks"])


def test_official_patches_only_change_tests_and_apply_to_fixtures(tmp_path):
    manifest = MODULE.load_manifest()
    for task in manifest["tasks"]:
        patch = Path(task["official_test_patch"]).resolve()
        paths = MODULE.test_patch_paths(patch)
        assert paths
        assert all(path.startswith(("test/", "tests/")) for path in paths)
        workspace = tmp_path / task["id"]
        shutil.copytree(task["fixture_repo"], workspace)
        MODULE.apply_patch(workspace, patch)
        assert all((workspace / path).is_file() for path in paths)


def test_official_patch_path_validator_rejects_production_changes(tmp_path):
    patch = tmp_path / "bad.patch"
    patch.write_text(
        "diff --git a/src/package.py b/src/package.py\n"
        "--- a/src/package.py\n"
        "+++ b/src/package.py\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-test path"):
        MODULE.test_patch_paths(patch)


def test_manifest_json_is_strictly_parseable():
    payload = json.loads(Path("validation/official_public_tests.json").read_text())
    assert payload["schema_version"] == "official-public-tests-v2"
