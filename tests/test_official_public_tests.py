import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("scripts/run_official_public_tests.py")
SPEC = importlib.util.spec_from_file_location("run_official_public_tests", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_official_public_manifest_is_bound_and_explicit():
    manifest = MODULE.load_manifest()
    assert len(manifest["tasks"]) == 5
    assert {
        task["id"]: task["pre_fix_expected"] for task in manifest["tasks"]
    } == {
        "click_empty_bytes_echo": "fail",
        "packaging_non_string_version": "fail",
        "werkzeug_float_url_notation": "fail",
        "jinja_overlay_async_default": "pass",
        "urllib3_port_zero": "fail",
    }
    assert all(len(task["source_commit"]) == 40 for task in manifest["tasks"])
    assert all(len(task["official_fix_commit"]) == 40 for task in manifest["tasks"])


def test_official_patches_only_change_tests_and_apply_to_fixtures():
    manifest = MODULE.load_manifest()
    for task in manifest["tasks"]:
        patch = Path(task["official_test_patch"]).resolve()
        paths = MODULE.test_patch_paths(patch)
        assert paths
        assert all(path.startswith(("test/", "tests/")) for path in paths)
        result = subprocess.run(
            ["git", "apply", "--check", "--unidiff-zero", str(patch)],
            cwd=task["fixture_repo"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


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


def test_source_snapshot_digest_ignores_runtime_artifacts(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    before = MODULE.source_snapshot_digest(tmp_path)
    (tmp_path / ".pico" / "runs").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "event.json").write_text("{}\n", encoding="utf-8")
    assert MODULE.source_snapshot_digest(tmp_path) == before


def test_manifest_json_is_strictly_parseable():
    payload = json.loads(Path("validation/official_public_tests.json").read_text())
    assert payload["schema_version"] == "official-public-tests-v1"
