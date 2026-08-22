import subprocess
from pathlib import Path

from scripts.materialize_real_oss import load_manifest as load_real_manifest
from scripts.run_click_triage import TASK_ID, prepare_click_workspace
from scripts.run_official_public_tests import load_manifest as load_official_manifest


def test_click_triage_workspace_is_a_clean_failing_baseline(tmp_path):
    real = load_real_manifest(Path("validation/real_oss_suite.json"))
    real_task = next(task for task in real["tasks"] if task["id"] == TASK_ID)
    official = load_official_manifest(Path("validation/official_public_tests.json"))
    official_task = next(task for task in official["tasks"] if task["id"] == TASK_ID)

    baseline = prepare_click_workspace(tmp_path / "click", real_task, official_task)

    assert len(baseline) == 40
    assert "BytesIO" in (tmp_path / "click" / "tests" / "test_utils.py").read_text(
        encoding="utf-8"
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path / "click",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""
