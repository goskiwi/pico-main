import json

import pytest

from pico.context_manager import ContextManager
from pico.models import FakeModelClient
from pico.skills import load_skills, select_skills_with_model
from tests.helpers import build_agent


def write_skill(root, name, text):
    path = root / ".pico" / "skills" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")


def test_load_skills_reads_skill_md_with_frontmatter(tmp_path):
    write_skill(
        tmp_path,
        "review",
        """---
name: code-review
description: Review Python code for regressions and missing tests.
---

# Code Review

Check behavior, tests, and safety.
""",
    )

    skills = load_skills(tmp_path)

    assert len(skills) == 1
    assert skills[0]["name"] == "code-review"
    assert skills[0]["path"] == ".pico/skills/review/SKILL.md"
    assert "Review Python code" in skills[0]["description"]
    assert "Check behavior" in skills[0]["content"]


def test_select_skills_with_model_uses_valid_json_names(tmp_path):
    write_skill(
        tmp_path,
        "debugging",
        """---
name: systematic-debugging
description: Investigate failing tests before changing code.
---

# Debugging

Reproduce the failure and isolate the cause.
""",
    )
    write_skill(
        tmp_path,
        "release",
        """---
name: release
description: Prepare release notes.
---

# Release
""",
    )

    model = FakeModelClient(['{"selected_names":["systematic-debugging"]}'])
    selected = select_skills_with_model(model, load_skills(tmp_path), "please debug the failing tests")

    assert [skill["name"] for skill in selected] == ["systematic-debugging"]
    assert "systematic-debugging" in model.prompts[0]
    assert "release" in model.prompts[0]


def test_select_skills_with_model_filters_invalid_names_and_limit(tmp_path):
    for name in ["debugging", "code-review", "test-driven-development"]:
        write_skill(
            tmp_path,
            name,
            f"""---
name: {name}
description: {name} skill.
---

# {name}
""",
        )

    model = FakeModelClient(['{"selected_names":["missing","debugging","code-review","test-driven-development"]}'])
    selected = select_skills_with_model(model, load_skills(tmp_path), "debug failing tests", limit=2)

    assert [skill["name"] for skill in selected] == ["debugging", "code-review"]


def test_select_skills_with_model_returns_empty_for_bad_json(tmp_path):
    write_skill(
        tmp_path,
        "debugging",
        """---
name: debugging
description: Debug failing tests.
---

# Debugging
""",
    )

    model = FakeModelClient(["not json"])
    selected = select_skills_with_model(model, load_skills(tmp_path), "测试失败了，帮我定位原因")

    assert selected == []


def test_select_skills_with_model_raises_model_errors(tmp_path):
    write_skill(
        tmp_path,
        "debugging",
        """---
name: debugging
description: Debug failing tests.
---

# Debugging
""",
    )

    model = FakeModelClient([])
    with pytest.raises(RuntimeError, match="fake model ran out of outputs"):
        select_skills_with_model(model, load_skills(tmp_path), "debug failing tests")


def test_context_manager_injects_matching_skills_between_memory_and_history(tmp_path):
    write_skill(
        tmp_path,
        "tdd",
        """---
name: tdd
description: Use for implementing features with tests first.
---

# TDD

Write a failing test before production code.
""",
    )
    agent = build_agent(tmp_path, ['{"selected_names":["tdd"]}'])

    prompt, metadata = ContextManager(agent).build("implement a new feature with tests")

    assert prompt.index("Working") < prompt.index("Skills:")
    assert prompt.index("Skills:") < prompt.index("Relevant memory:")
    assert "Skill: tdd" in prompt
    assert "Write a failing test before production code." in prompt
    assert metadata["skills"]["selected_names"] == ["tdd"]
    assert metadata["sections"]["skills"]["rendered_estimated_tokens"] <= metadata["sections"]["skills"]["budget_tokens"]
    assert metadata["section_order"] == ["prefix", "memory", "skills", "relevant_memory", "history", "current_request"]


def test_context_manager_omits_skills_when_none_match(tmp_path):
    write_skill(
        tmp_path,
        "release",
        """---
name: release
description: Prepare release notes.
---

# Release
""",
    )
    agent = build_agent(tmp_path, ['{"selected_names":[]}'])

    prompt, metadata = ContextManager(agent).build("summarize README")

    assert "Skills:" not in prompt
    assert metadata["skills"]["selected_names"] == []
    assert metadata["sections"]["skills"]["rendered_chars"] == 0


def test_context_manager_clips_long_skill_content_to_budget(tmp_path):
    write_skill(
        tmp_path,
        "large",
        """---
name: large-skill
description: Use this large skill for implementation.
---

"""
        + ("Long guidance. " * 500),
    )
    agent = build_agent(tmp_path, ['{"selected_names":["large-skill"]}'])

    _, metadata = ContextManager(
        agent,
        total_budget=500,
        section_budgets={
            "prefix": 80,
            "memory": 60,
            "skills": 50,
            "relevant_memory": 60,
            "history": 80,
        },
    ).build("implementation task")

    assert metadata["skills"]["selected_names"] == ["large-skill"]
    assert metadata["sections"]["skills"]["rendered_estimated_tokens"] <= 50
    assert metadata["sections"]["skills"]["rendered_chars"] < metadata["sections"]["skills"]["raw_chars"]


def test_run_report_records_selected_skills(tmp_path):
    write_skill(
        tmp_path,
        "debugging",
        """---
name: debugging
description: Debug failing tests.
---

# Debugging
""",
    )
    agent = build_agent(tmp_path, ['{"selected_names":["debugging"]}', "<final>Inspected.</final>"])

    assert agent.ask("debug failing tests") == "Inspected."

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["skills"]["selected_names"] == ["debugging"]
    assert report["summary"]["skills"] == ["debugging"]
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events[-1]["prompt_metadata"]["skills"]["selected_names"] == ["debugging"]
