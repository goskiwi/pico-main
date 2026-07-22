"""Opt-in smoke coverage for the real delegate scheduler path.

This is deliberately outside the offline suite: it exercises the configured
Responses endpoint, independent child clients, and ``delegate_many`` together.
"""

from pathlib import Path
import os
import re

import pytest

from pico.cli import DEFAULT_OPENAI_MODEL, _load_workspace_env
from pico.models import OpenAICompatibleModelClient
from tests.helpers import build_agent


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _live_model_config():
    env = _load_workspace_env(PROJECT_ROOT)
    live_enabled = env.get("PICO_RUN_LIVE_TESTS") == "1" or os.environ.get(
        "PICO_RUN_LIVE_TESTS"
    ) == "1"
    if not live_enabled:
        pytest.skip("set PICO_RUN_LIVE_TESTS=1 to run live tests")
    missing = [
        name
        for name in ("OPENAI_API_BASE", "OPENAI_API_KEY")
        if not env.get(name)
    ]
    if missing:
        pytest.skip(f"project .env.local is missing: {', '.join(missing)}")
    return env


@pytest.mark.live
def test_live_delegate_many_smoke_uses_independent_real_children(tmp_path):
    env = _live_model_config()
    agent = build_agent(
        tmp_path,
        [],
        approval_policy="never",
        feature_flags={"llm_memory_extract": False, "llm_history_compaction": False},
    )
    agent.model_client = OpenAICompatibleModelClient(
        model=env.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        base_url=env["OPENAI_API_BASE"],
        api_key=env["OPENAI_API_KEY"],
        temperature=0.0,
        timeout=120,
    )

    result = agent.run_tool(
        "delegate_many",
        {
            "tasks": [
                {
                    "role": "explore",
                    "task": "Read README.md and report its first factual sentence in one sentence.",
                    "max_steps": 3,
                },
                {
                    "role": "review",
                    "task": "Read README.md and report one concrete maintenance risk in one sentence.",
                    "max_steps": 3,
                },
            ]
        },
    )

    assert agent._last_tool_result_metadata["tool_status"] == "ok"
    delegate_outcome = agent._last_tool_result_metadata["delegate_outcome"]
    assert delegate_outcome["requested_count"] == 2
    assert delegate_outcome["completed_count"] == 2
    assert delegate_outcome["failed_count"] == 0
    assert [item["status"] for item in delegate_outcome["items"]] == ["ok", "ok"]
    assert [item["child_status"] for item in delegate_outcome["items"]] == [
        "completed",
        "completed",
    ]
    child_agent_ids = [item["agent_id"] for item in delegate_outcome["items"]]
    assert all(child_agent_ids)
    assert len(set(child_agent_ids)) == 2
    assert "delegate_many_result count=2" in result
    assert len(re.findall(r"^--- child \d+ role=", result, flags=re.MULTILINE)) == 2
    assert "status=error" not in result
    assert "status=timeout" not in result
    assert "status=budget_exhausted" not in result
