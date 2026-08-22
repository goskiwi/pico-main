import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pico import ModelAction, WorkingState
from pico.compaction_brief import (
    SUMMARY_TOOL,
    CompactionBrief,
    SemanticBriefSummarizer,
)


def valid_brief():
    return {
        "goal": "Fix normalization",
        "constraints_preferences": ["Modify one file"],
        "progress": {
            "done": ["Read evidence"],
            "in_progress": ["Patch target"],
            "blocked": [],
        },
        "key_decisions": ["Use split and join"],
        "next_steps": ["Run verifier"],
        "critical_context": ["FINAL_RESPONSE_TOKEN: ORBIT-DELTA-7319"],
    }


def test_semantic_brief_requires_and_renders_all_six_sections():
    rendered = CompactionBrief.from_dict(valid_brief()).render()

    assert rendered.count("\n## ") == 5
    for heading in (
        "Goal",
        "Constraints & Preferences",
        "Progress",
        "Key Decisions",
        "Next Steps",
        "Critical Context",
    ):
        assert f"## {heading}" in rendered
    assert "ORBIT-DELTA-7319" in rendered


def test_semantic_brief_rejects_missing_or_extra_fields():
    missing = valid_brief()
    missing.pop("critical_context")
    with pytest.raises(ValueError, match="invalid fields"):
        CompactionBrief.from_dict(missing)

    extra = valid_brief()
    extra["notes"] = []
    with pytest.raises(ValueError, match="invalid fields"):
        CompactionBrief.from_dict(extra)


def test_summary_tool_schema_is_strict_and_complete():
    schema = SUMMARY_TOOL["parameters"]

    assert SUMMARY_TOOL["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    progress = schema["properties"]["progress"]
    assert progress["additionalProperties"] is False
    assert set(progress["required"]) == set(progress["properties"])


def test_summarizer_uses_isolated_structured_model_request():
    class SummaryClient:
        def __init__(self):
            self.last_completion_metadata = {"input_tokens": 100, "output_tokens": 20}

        def complete_action(self, prompt, max_new_tokens, *, action_tools, **_kwargs):
            assert "FINAL_RESPONSE_TOKEN" in prompt
            assert max_new_tokens == 2048
            assert action_tools == [SUMMARY_TOOL]
            return ModelAction.tool("submit_compaction_brief", valid_brief())

    summarizer = SemanticBriefSummarizer(SummaryClient)
    event = SimpleNamespace(
        kind="tool_result",
        name="read_file",
        args={"path": "evidence/segment_01.md"},
        content="FINAL_RESPONSE_TOKEN: ORBIT-DELTA-7319",
    )

    rendered = summarizer.summarize(
        (event,),
        WorkingState(goal="Fix normalization"),
    )

    assert "## Critical Context" in rendered
    assert "ORBIT-DELTA-7319" in rendered
    assert summarizer.calls[0]["completion_metadata"]["input_tokens"] == 100


def test_published_semantic_compaction_ab_supports_core_decision():
    artifact = json.loads(Path("artifacts/semantic-compaction-ab.json").read_text())

    assert artifact["passed"] is True
    comparison = artifact["comparison"]
    assert comparison["baseline_task_passed"] is True
    assert comparison["semantic_task_passed"] is True
    assert comparison["baseline_critical_token_retained"] is False
    assert comparison["semantic_critical_token_retained"] is True
    assert comparison["semantic_summary_request_count"] >= 1
