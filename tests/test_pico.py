from pathlib import Path

import pico as mini_pkg
from pico import tools as toolkit
from pico import (
    FakeModelClient,
    Pico,
    ModelAction,
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
    tool_items = [item for item in agent.session["history"] if item["role"] == "tool" and item["name"] == "read_file"]
    assert tool_items
    assert tool_items[0]["node_id"] == "t001_read_file"
    assert tool_items[0]["content_ref"].endswith("tool_outputs/0001_read_file.txt")
    assert "hello.txt" in agent.session["memory"]["working"]["recent_files"]

    graph = (agent.current_run_dir / "task_graph.mmd").read_text(encoding="utf-8")
    assert 't001_read_file["tool | ok | read_file hello.txt"]' in graph
    assert "%% t001_read_file ref:" in graph
    assert "tool_outputs/0001_read_file.txt" in graph


def test_native_action_loop_reuses_the_structured_conversation_prompt(tmp_path):
    class NativeModelClient:
        supports_native_actions = True
        supports_prompt_cache = False

        def __init__(self):
            self.prompts = []
            self.results = []
            self.last_completion_metadata = {}
            self.actions = [
                ModelAction.tool(
                    "read_file",
                    {"path": "README.md", "start": 1, "end": 1},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.final(
                    "Finished.",
                    protocol="responses_function",
                    call_id="call_2",
                ),
            ]

        def reset_action_session(self):
            self.results = []

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del max_new_tokens, kwargs
            self.prompts.append(prompt)
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [])
    client = NativeModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect README.md") == "Finished."

    assert len(client.prompts) == 2
    assert client.prompts[0] == client.prompts[1]
    assert client.results[0][0] == "call_1"
    assert agent.last_prompt_metadata["prompt_reused"] is True


def test_prefix_requires_a_test_for_interacting_constraints(tmp_path):
    agent = build_agent(tmp_path, [])

    assert "verify every explicit behavioral constraint" in agent.prefix
    assert "discriminating test that exercises the interaction" in agent.prefix


def test_native_action_gets_one_final_only_turn_after_tool_limit(tmp_path):
    class NativeModelClient:
        supports_native_actions = True
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}
            self.tool_sets = []
            self.actions = [
                ModelAction.tool(
                    "list_files",
                    {"path": "."},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.final(
                    "Finished at the tool limit.",
                    protocol="responses_function",
                    call_id="call_2",
                ),
            ]

        def reset_action_session(self):
            pass

        def complete_action(self, prompt, max_new_tokens, *, action_tools, **kwargs):
            del prompt, max_new_tokens, kwargs
            self.tool_sets.append([tool["name"] for tool in action_tools])
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            del action, result

    agent = build_agent(tmp_path, [], max_steps=1)
    client = NativeModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    answer = agent.ask("Inspect once, then finish")

    assert answer == "Finished at the tool limit."
    assert "list_files" in client.tool_sets[0]
    assert client.tool_sets[1] == ["submit_final"]
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.attempts == 2


def test_final_only_turn_cannot_execute_another_tool(tmp_path):
    class NativeModelClient:
        supports_native_actions = True
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}
            self.tool_sets = []
            self.actions = [
                ModelAction.tool(
                    "list_files",
                    {"path": "."},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.tool(
                    "write_file",
                    {"path": "too-late.txt", "content": "blocked"},
                    protocol="responses_function",
                    call_id="call_2",
                ),
            ]

        def reset_action_session(self):
            pass

        def complete_action(self, prompt, max_new_tokens, *, action_tools, **kwargs):
            del prompt, max_new_tokens, kwargs
            self.tool_sets.append([tool["name"] for tool in action_tools])
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            del action, result

    agent = build_agent(tmp_path, [], max_steps=1)
    client = NativeModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    answer = agent.ask("Try to exceed the tool budget")

    assert answer == "Stopped after reaching the step limit without a final answer."
    assert client.tool_sets[1] == ["submit_final"]
    assert not (tmp_path / "too-late.txt").exists()
    assert [entry["name"] for entry in agent.tool_audit_log] == ["list_files"]


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
    assert "not available for the active skills" in tool_items[-1]["summary"]
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


def test_prompt_teaches_read_tool_output_for_task_graph_refs(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])

    prompt = agent.prompt("continue previous run")

    assert "- read_tool_output(" in prompt
    assert "When a task graph node has a ref, use read_tool_output instead of manually reading tool_outputs paths." in prompt


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

    resumed = Pico.from_session(
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

    resumed = Pico.from_session(
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


def test_explicit_final_mode_retries_bare_action_narration(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "I will update the file next.",
            "<final>Finished after the runtime requested an explicit final.</final>",
        ],
        feature_flags={"require_explicit_final": True},
    )

    answer = agent.ask("Do the task")

    assert answer == "Finished after the runtime requested an explicit final."
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("bare text is not a final answer" in item for item in notices)


def test_agent_retries_unclosed_protocol_tags(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"broken.txt","content":"x"}}',
            "<final>Recovered from an unclosed tool tag.</final>",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered from an unclosed tool tag."
    assert not (tmp_path / "broken.txt").exists()
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("unclosed <tool>" in item for item in notices)


def test_workspace_change_mode_rejects_premature_final(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Done.</final>",
            '<tool name="write_file" path="result.txt"><content>complete\n</content></tool>',
            "<final>Created result.txt.</final>",
        ],
        feature_flags={"require_workspace_change": True},
    )

    answer = agent.ask("Create result.txt")

    assert answer == "Created result.txt."
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "complete\n"
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("no effective file change" in item for item in notices)


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
    assert len(agent.model_action_rejections) == 1
    assert agent.model_action_rejections[0]["protocol"] == "scripted_text"
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["summary"]["model_action_rejection_count"] == 1
    assert report["model_action_rejections"][0]["raw_preview"].startswith("<tool>")


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
    assert not hasattr(Pico, "parse")


def test_short_read_summary_keeps_the_complete_file(tmp_path):
    agent = build_agent(tmp_path, [])
    result = "\n".join(f"{line}: value" for line in range(1, 13))

    summary = agent.summarize_tool_result("read_file", {"path": "small.py"}, result)

    assert "1: value" in summary
    assert "12: value" in summary
    assert "omitted" not in summary
    assert not hasattr(Pico, "parse_xml_tool")


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
    assert not any(hasattr(Pico, name) for name in legacy_names)


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
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_approval_api():
    assert not hasattr(Pico, "approve")


def test_runtime_does_not_expose_legacy_report_api():
    legacy_names = [
        "build_report",
        "record_tool_audit",
        "build_run_summary",
    ]
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_workspace_diff_api():
    legacy_names = [
        "capture_workspace_snapshot",
        "diff_workspace_snapshots",
    ]
    assert not any(hasattr(Pico, name) for name in legacy_names)


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
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["<final>First pass.</final>"])
    assert agent.ask("Start a session") == "First pass."

    resumed = Pico.from_session(
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
    assert "delegate_result role=explore" in tool_events[0]["summary"]


def test_native_delegate_uses_independent_model_client(tmp_path):
    class NativeParentClient:
        supports_native_actions = True

        def __init__(self):
            self.fork_count = 0

        def fork_for_delegate(self):
            self.fork_count += 1
            return FakeModelClient(["<final>Child result.</final>"])

    agent = build_agent(tmp_path, [])
    parent_client = NativeParentClient()
    agent.model_client = parent_client

    result = toolkit.run_delegate_child(
        agent, {"role": "explore", "task": "inspect README", "max_steps": 2}
    )

    assert parent_client.fork_count == 1
    assert result["answer"] == "Child result."


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
    assert "delegate_many_result count=2" in tool_events[0]["summary"]
    assert "role=explore" in tool_events[0]["summary"]
    assert "Explore result." in tool_events[0]["summary"]
    assert "role=review" in tool_events[0]["summary"]
    assert "Review result." in tool_events[0]["summary"]


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
    assert mini_pkg.Pico is not None
    assert not hasattr(mini_pkg, "MiniAgent")
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
