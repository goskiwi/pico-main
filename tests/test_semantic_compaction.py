import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pico import ModelAction
from pico.compaction_summary import (
    SUMMARY_TOOL,
    CompactionSummarizer,
    CompactionSummary,
)


def valid_summary():
    return {
        "progress": {
            "done": ["Read evidence"],
            "in_progress": ["Patch target"],
            "blocked": [],
        },
        "critical_context": ["FINAL_RESPONSE_TOKEN: ORBIT-DELTA-7319"],
    }


def test_semantic_summary_contains_only_historical_sections():
    rendered = CompactionSummary.from_dict(valid_summary()).render()

    for heading in ("Progress", "Critical Context"):
        assert f"## {heading}" in rendered
    for heading in ("Goal", "Constraints & Preferences", "Key Decisions", "Next Steps"):
        assert f"## {heading}" not in rendered
    assert "ORBIT-DELTA-7319" in rendered


def test_semantic_summary_rejects_missing_or_extra_fields():
    missing = valid_summary()
    missing.pop("critical_context")
    with pytest.raises(ValueError, match="invalid fields"):
        CompactionSummary.from_dict(missing)

    extra = valid_summary()
    extra["notes"] = []
    with pytest.raises(ValueError, match="invalid fields"):
        CompactionSummary.from_dict(extra)


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

        def complete_action(
            self,
            prompt,
            max_new_tokens,
            *,
            instructions,
            action_tools,
            **_kwargs,
        ):
            assert "FINAL_RESPONSE_TOKEN" in prompt
            assert "Do not restate" in instructions
            assert max_new_tokens == 2048
            assert action_tools == [SUMMARY_TOOL]
            return ModelAction.tool("submit_compaction_summary", valid_summary())

    summarizer = CompactionSummarizer(SummaryClient)
    event = SimpleNamespace(
        kind="tool_result",
        name="read_file",
        args={"path": "evidence/segment_01.md"},
        content="FINAL_RESPONSE_TOKEN: ORBIT-DELTA-7319",
    )

    rendered = summarizer.summarize(
        (event,),
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
