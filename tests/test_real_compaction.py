import json
from pathlib import Path

from scripts.run_real_compaction import (
    EVIDENCE_COUNT,
    TARGET_PATH,
    build_prompt,
    prepare_workspace,
)


def test_real_compaction_workspace_is_reproducible_and_large(tmp_path):
    workspace = prepare_workspace(tmp_path / "compaction")
    evidence = sorted((workspace / "evidence").glob("segment_*.md"))

    assert len(evidence) == EVIDENCE_COUNT
    assert all(path.stat().st_size > 7000 for path in evidence)
    assert "return value.strip().lower()" in (workspace / TARGET_PATH).read_text()


def test_real_compaction_prompt_requires_ordered_reads_and_one_write_scope():
    prompt = build_prompt()

    assert prompt.count("- evidence/segment_") == EVIDENCE_COUNT
    assert "read every listed evidence" in prompt
    assert f"modify only {TARGET_PATH}" in prompt
    assert "The Runtime owns verification" in prompt


def test_published_real_compaction_artifact_passes_all_checks():
    artifact = json.loads(Path("artifacts/real-compaction.json").read_text())

    assert artifact["passed"] is True
    assert artifact["analysis"]["compaction_count"] >= 1
    assert artifact["analysis"]["provider_session_reset_count"] >= 1
    assert len(artifact["analysis"]["evidence_read_paths"]) == EVIDENCE_COUNT
    assert all(artifact["checks"].values())
