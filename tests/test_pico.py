from pathlib import Path

import pico as mini_pkg
from pico import (
    FakeModelClient,
    MiniAgent,
    build_welcome,
)
from tests.helpers import build_agent


def write_skill(root, name, text):
    path = root / ".pico" / "skills" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")


def test_agent_runs_tool_then_final(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}</tool>',
            "<final>Read the file successfully.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Read the file successfully."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    assert "hello.txt" in agent.session["memory"]["working"]["recent_files"]


def test_skill_tool_whitelist_rejects_undeclared_tool(tmp_path):
    write_skill(
        tmp_path,
        "review",
        """---
name: review
description: Review code without editing.
tools: read_file, search
trigger_keywords: review
---

# Review
""",
    )
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"out.txt","content":"bad"}}</tool>',
            "<final>Stopped editing.</final>",
        ],
    )

    assert agent.ask("review README") == "Stopped editing."

    tool_items = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_items[-1]["name"] == "write_file"
    assert "not available for the active skills" in tool_items[-1]["content"]
    assert not (tmp_path / "out.txt").exists()


def test_strict_skill_filters_tools_from_prompt(tmp_path):
    write_skill(
        tmp_path,
        "readonly",
        """---
name: readonly
description: Read files only.
tools: read_file
allowed_tools_strict: true
trigger_keywords: inspect
---

# Readonly
""",
    )
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
        ],
    )

    assert agent.ask("inspect hello.txt") == "Done."

    prompt = agent.model_client.prompts[0]
    assert "- read_file(" in prompt
    assert "- write_file(" not in prompt
    assert "- run_shell(" not in prompt


def test_agent_updates_goal_on_each_request(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>First pass.</final>",
            "<final>Second pass.</final>",
        ],
    )

    assert agent.ask("First request") == "First pass."
    assert agent.session["memory"]["working"]["goal"] == "First request"

    assert agent.ask("Second request") == "Second pass."
    assert agent.session["memory"]["working"]["goal"] == "Second request"


def test_agent_marks_working_state_completed_after_final_answer(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])

    assert agent.ask("Finish cleanly") == "Done."

    working = agent.session["memory"]["working"]
    assert working["goal"] == "Finish cleanly"
    assert working["current_subtask"] == "completed"
    assert working["next_action"] == "-"
    assert working["last_error"] == ""


def test_agent_records_tool_failure_in_working_state(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"missing.txt","start":1,"end":1}}</tool>',
            "<final>Recovered.</final>",
        ],
    )

    assert agent.ask("Inspect missing.txt") == "Recovered."

    process_notes = [note["text"] for note in agent.session["memory"]["episodic_notes"] if note["kind"] == "process"]
    assert any("read_file rejected" in note for note in process_notes)


def test_agent_records_retry_in_working_state_until_recovery(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "<final>Recovered after retry.</final>",
        ],
    )

    assert agent.ask("Recover from empty output") == "Recovered after retry."

    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("empty response" in item for item in notices)


def test_agent_records_stopped_state_when_step_limit_is_reached(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        ['<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>'],
        max_steps=1,
    )

    answer = agent.ask("Inspect once")

    working = agent.session["memory"]["working"]
    assert answer == "Stopped after reaching the step limit without a final answer."
    assert working["current_subtask"] == "stopped"
    assert working["next_action"] == "-"
    assert "step limit" in working["last_error"]


def test_agent_only_stores_reusable_epistemic_notes(tmp_path):
    (tmp_path / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"facts.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
            "<final>It is red.</final>",
        ],
    )

    assert agent.ask("Read the file and remember the fact") == "Done."
    notes = agent.session["memory"]["episodic_notes"]
    assert any("deploy key is red" in note["text"] for note in notes)
    assert not any(note["text"] == "Done." for note in notes)
    assert not any(note["text"] == "Done." for note in notes)

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>It is red.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("What color is the deploy key?") == "It is red."
    prompt = resumed.model_client.prompts[0]
    assert "Relevant memory" in prompt
    assert "deploy key is red" in prompt


def test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    agent.memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    agent.memory.remember_file("./sample.txt")
    assert agent.memory.to_dict()["file_summaries"]["sample.txt"]["freshness"]

    rendered = agent.memory.render_memory_text()
    assert "File Summaries" in rendered
    assert "- sample.txt: sample.txt: alpha" in rendered
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert "sample.txt: alpha" not in resumed.memory_text()
    resumed.memory.invalidate_file_summary("sample.txt")
    assert "sample.txt" not in resumed.memory.to_dict()["file_summaries"]


def test_agent_retries_after_empty_model_output(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "<final>Recovered after retry.</final>",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after retry."
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("empty response" in item for item in notices)


def test_agent_retries_after_malformed_tool_payload(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":"bad"}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Recovered after malformed tool output.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after malformed tool output."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("valid <tool> call" in item for item in notices)


def test_retries_do_not_consume_the_whole_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "",
            "<final>Recovered after several retries.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after several retries."


def test_runtime_does_not_expose_legacy_parse_api():
    assert not hasattr(MiniAgent, "parse")
    assert not hasattr(MiniAgent, "parse_xml_tool")


def test_runtime_does_not_expose_legacy_security_api():
    legacy_names = [
        "redact_text",
        "redact_artifact",
        "looks_sensitive_env_name",
        "is_secret_env_name",
        "configured_secret_env_items",
        "detected_secret_env_items",
        "secret_env_summary",
        "detected_secret_env_summary",
        "shell_env",
    ]
    assert not any(hasattr(MiniAgent, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_memory_promotion_api():
    legacy_names = [
        "reject_durable_reason",
        "extract_durable_promotions",
        "promote_durable_memory",
        "llm_memory_index_text",
        "build_memory_extractor_prompt",
        "parse_memory_extractor_output",
        "llm_promote_durable_memory",
    ]
    assert not any(hasattr(MiniAgent, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_approval_api():
    assert not hasattr(MiniAgent, "approve")


def test_runtime_does_not_expose_legacy_report_api():
    legacy_names = [
        "build_report",
        "record_tool_audit",
        "build_run_summary",
    ]
    assert not any(hasattr(MiniAgent, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_workspace_diff_api():
    legacy_names = [
        "capture_workspace_snapshot",
        "diff_workspace_snapshots",
    ]
    assert not any(hasattr(MiniAgent, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_tool_policy_api():
    legacy_names = [
        "tool_capability",
        "tool_risk_level",
        "tool_permission_error",
        "dry_run_tool_result",
        "shell_policy_metadata",
        "shell_command_policy",
        "repeated_tool_call",
    ]
    assert not any(hasattr(MiniAgent, name) for name in legacy_names)


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["<final>First pass.</final>"])
    assert agent.ask("Start a session") == "First pass."

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.session["history"][0]["content"] == "Start a session"
    assert resumed.ask("Continue") == "Resumed."


def test_delegate_uses_child_agent(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"role":"explore","task":"inspect README","max_steps":2}}</tool>',
            "<final>Child result.</final>",
            "<final>Parent incorporated the child result.</final>",
        ],
    )

    answer = agent.ask("Use delegation")

    assert answer == "Parent incorporated the child result."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result role=explore" in tool_events[0]["content"]


def test_delegate_many_uses_multiple_child_agents(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate_many","args":{"tasks":[{"role":"explore","task":"inspect README","max_steps":2},{"role":"review","task":"review README","max_steps":2}]}}</tool>',
            "<final>Explore result.</final>",
            "<final>Review result.</final>",
            "<final>Parent incorporated the child results.</final>",
        ],
    )

    answer = agent.ask("Use multiple delegates")

    assert answer == "Parent incorporated the child results."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate_many"
    assert "delegate_many_result count=2" in tool_events[0]["content"]
    assert "role=explore" in tool_events[0]["content"]
    assert "Explore result." in tool_events[0]["content"]
    assert "role=review" in tool_events[0]["content"]
    assert "Review result." in tool_events[0]["content"]


def test_welcome_screen_keeps_box_shape_for_long_paths(tmp_path):
    deep = tmp_path / "very" / "long" / "path" / "for" / "the" / "mini" / "agent" / "welcome" / "screen"
    deep.mkdir(parents=True)
    agent = build_agent(deep, [])

    welcome = build_welcome(agent, model="qwen3.5:4b", host="http://127.0.0.1:11434")
    lines = welcome.splitlines()

    assert len(lines) >= 5
    assert len({len(line) for line in lines}) == 1
    assert "..." in welcome
    assert "(  o o  )" in welcome
    assert "MINI-CODING-AGENT" not in welcome
    assert "MINI CODING AGENT" not in welcome
    assert "pico" in welcome
    assert "local coding agent" in welcome
    assert "// READY" not in welcome
    assert "SLASH" not in welcome
    assert "READY      " not in welcome
    assert "commands: Commands:" not in welcome


def test_public_api_exports_resolve_through_package_path():
    assert callable(mini_pkg.build_welcome)
    assert mini_pkg.FakeModelClient is not None
    assert mini_pkg.MiniAgent is not None
    assert mini_pkg.OllamaModelClient is not None
    assert mini_pkg.SessionStore is not None
    assert mini_pkg.WorkspaceContext is not None
    assert Path(mini_pkg.__file__).as_posix().endswith("/pico/__init__.py")


def test_reviewer_skeleton_docs_exist():
    review_pack = Path("docs/review-pack/README.md")
    architecture = Path("docs/architecture/agent-harness-v1-overview.md")

    assert review_pack.exists()
    assert architecture.exists()

    review_text = review_pack.read_text(encoding="utf-8")
    assert "Project pitch" in review_text
    assert "Architecture map" in review_text
    assert "Benchmark evidence" in review_text
    assert "Sample run artifact list" in review_text

    architecture_text = architecture.read_text(encoding="utf-8")
    assert "Agent Harness v1" in architecture_text
    assert "task state" in architecture_text.lower()
