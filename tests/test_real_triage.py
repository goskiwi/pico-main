import subprocess
from pathlib import Path

from scripts.materialize_real_oss import load_manifest as load_real_manifest
from scripts.run_official_public_tests import load_manifest as load_official_manifest
from scripts.run_real_triage import prepare_triage_workspace, visible_command

TASK_ID = "click_empty_bytes_echo"


def test_real_triage_workspace_is_a_clean_failing_baseline(tmp_path):
    real = load_real_manifest(Path("validation/real_oss_suite.json"))
    real_task = next(task for task in real["tasks"] if task["id"] == TASK_ID)
    official = load_official_manifest(Path("validation/official_public_tests.json"))
    official_task = next(task for task in official["tasks"] if task["id"] == TASK_ID)

    baseline = prepare_triage_workspace(tmp_path / "click", real_task, official_task)

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
    command = visible_command(official_task)
    assert command.startswith("PYTHONPATH=src python -m pytest")
    assert official_task["official_test_nodes"][0] in command
