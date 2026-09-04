"""Historical report assertions, not executions of the current Runtime or LLM."""

import json
from pathlib import Path

import pytest

from scripts.run_real_compaction import EVIDENCE_COUNT
from scripts.run_real_system import TARGET_PATH

pytestmark = pytest.mark.archived_report


def test_archived_real_cli_system_artifact_passes_every_boundary():
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
    assert (
        artifact["checks"]["repository_instruction_followed_without_file_read"] is True
    )
    assert artifact["checks"]["target_located_without_prompt_hint"] is True
    assert "Status: completed" in artifact["cli"]["stdout"]
    assert "Verification: passed" in artifact["cli"]["stdout"]
    assert "AGENTS-FOLLOWED" in artifact["cli"]["stdout"]
    assert artifact["passed"] is True
    assert all(artifact["checks"].values())


def test_archived_real_child_artifact_uses_the_new_patch_receipt():
    artifact = json.loads(Path("artifacts/real-child.json").read_text())

    assert artifact["passed"] is True
    assert all(artifact["checks"].values())
    assert artifact["cli"]["exit_code"] == 0
    (receipt,) = artifact["analysis"]["child_receipts"]
    assert set(receipt) == {
        "child_id",
        "child_run_id",
        "role",
        "status",
        "result",
        "patch",
    }
    assert set(receipt["patch"]) == {
        "base_sha",
        "changed_paths",
        "sha256",
        "integrated",
    }
    assert receipt["patch"]["changed_paths"] == [TARGET_PATH]
    assert artifact["checks"]["parent_did_not_edit_directly"] is True


@pytest.mark.parametrize(
    "path",
    [
        "artifacts/real-ask.json",
        "artifacts/real-code-approval.json",
        "artifacts/real-revision-repair.json",
        "artifacts/real-resume.json",
    ],
)
def test_archived_real_harness_artifacts_pass_all_checks(path):
    artifact = json.loads(Path(path).read_text())

    assert artifact["model"]
    assert artifact["runtime"]["commit_sha"]
    assert artifact["runtime"]["working_tree_dirty"] is False
    assert artifact["passed"] is True
    assert all(artifact["checks"].values())


def test_archived_real_harness_artifacts_cover_distinct_runtime_paths():
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


def test_archived_real_compaction_artifact_passes_all_checks():
    artifact = json.loads(Path("artifacts/real-compaction.json").read_text())

    assert artifact["passed"] is True
    assert artifact["analysis"]["compaction_count"] >= 1
    assert artifact["analysis"]["provider_session_reset_count"] >= 1
    assert artifact["analysis"]["observation_batch_count"] == 3
    assert artifact["analysis"]["model_request_count"] <= 10
    assert len(artifact["analysis"]["evidence_read_paths"]) == EVIDENCE_COUNT
    assert all(artifact["checks"].values())
