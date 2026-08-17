import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path("scripts/run_real_oss_validation.py")
SPEC = importlib.util.spec_from_file_location("run_real_oss_validation", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SUITE_SPEC = importlib.util.spec_from_file_location(
    "run_real_oss_suite", Path("scripts/run_real_oss_suite.py")
)
SUITE_MODULE = importlib.util.module_from_spec(SUITE_SPEC)
SUITE_SPEC.loader.exec_module(SUITE_MODULE)


def test_real_oss_manifest_is_strict_and_points_to_frozen_upstream():
    manifest = Path("validation/real_oss_suite.json")
    task = MODULE.load_task(manifest, "click_empty_bytes_echo")

    assert task["id"] == "click_empty_bytes_echo"
    assert task["source_repository"] == "https://github.com/pallets/click.git"
    assert len(task["source_commit"]) == 40
    assert len(json.loads(manifest.read_text())["tasks"]) == 5
    assert "tests/**" in MODULE.FORBIDDEN_CHANGE_GLOBS


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
    assert MODULE.matches("src/code.py", ["src/**/*.py"])


def test_real_oss_publication_rejects_dirty_runtime():
    with pytest.raises(RuntimeError, match="requires a clean worktree"):
        MODULE.require_clean_runtime({"working_tree_dirty": True})


def test_provider_continuation_requires_a_reused_prompt_turn():
    events = [
        {"event_type": "prompt_built", "payload": {"prompt_metadata": {"prompt_reused": False}}},
        {"event_type": "prompt_built", "payload": {"prompt_metadata": {"prompt_reused": True}}},
    ]

    assert MODULE.provider_continuation_check(events) == {
        "ok": True,
        "prompt_builds": 2,
        "reused_turns": 1,
        "provider_session_resets": 0,
    }
    assert MODULE.provider_continuation_check(events[:1])["ok"] is False


def test_suite_retries_only_provider_infrastructure_errors():
    assert SUITE_MODULE.retryable_infrastructure_error(
        RuntimeError("OpenAI-compatible request failed with HTTP 503: unavailable")
    )
    assert SUITE_MODULE.retryable_infrastructure_error(
        RuntimeError("Could not reach the OpenAI-compatible backend")
    )
    assert not SUITE_MODULE.retryable_infrastructure_error(
        RuntimeError("hidden verifier failed")
    )

