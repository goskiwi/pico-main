import json
from unittest.mock import patch

from pico.actions import ModelAction
from pico.models import FakeModelClient
from pico.runtime import Pico
from pico.sandbox import SandboxResult
from tests.helpers import UnitTestSandbox, build_agent


def write_skill(root, name, text):
    path = root / ".pico" / "skills" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")


def test_agent_graph_disables_ambient_langsmith_tracing(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    agent = build_agent(tmp_path, ["<final>Private result.</final>"])

    with patch("langchain_core.tracers.langchain.LangChainTracer") as tracer:
        assert agent.ask("Do not trace this request") == "Private result."

    tracer.assert_not_called()


def test_agent_graph_stops_at_exact_retry_limit(tmp_path):
    malformed = '<tool>{"name":"read_file","args":"bad"}</tool>'
    agent = build_agent(tmp_path, [malformed] * 5, max_steps=1)

    answer = agent.ask("Keep returning malformed actions")

    assert answer.startswith("Stopped after too many malformed model responses")
    assert agent.current_task_state.attempts == 5
    assert agent.current_task_state.stop_reason == "retry_limit_reached"


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
    tool_items = [
        item
        for item in agent.session["history"]
        if item["role"] == "tool" and item["name"] == "read_file"
    ]
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


def test_pytest_output_keeps_failure_tail_in_context_and_full_artifact(tmp_path):
    class LongPytestSandbox(UnitTestSandbox):
        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout, env
            noise = "".join(f"noise line {index:04d}\n" for index in range(800))
            return SandboxResult(
                returncode=1,
                stdout=(
                    f"{noise}"
                    "FAILED tests/test_checkout.py::test_cross_module_total - AssertionError\n"
                    "=========================== short test summary info ===========================\n"
                    "FAILED tests/test_checkout.py::test_cross_module_total\n"
                    "1 failed, 7 passed in 0.42s\n"
                ),
            )

    class NativeModelClient:
        supports_native_actions = True
        supports_prompt_cache = False

        def __init__(self):
            self.results = []
            self.last_completion_metadata = {}
            self.actions = [
                ModelAction.tool(
                    "run_shell",
                    {"command": "pytest -q", "timeout": 20},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.final("Finished.", protocol="responses_function", call_id="call_2"),
            ]

        def reset_action_session(self):
            self.results = []

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [], sandbox=LongPytestSandbox(tmp_path))
    client = NativeModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Run pytest and inspect the failure.") == "Finished."

    compact_result = client.results[0][1]
    assert len(compact_result) <= 4000
    assert "FAILED tests/test_checkout.py::test_cross_module_total" in compact_result
    assert "1 failed, 7 passed in 0.42s" in compact_result
    assert "pytest output compacted" in compact_result

    audit = agent.tool_audit_log[0]
    assert audit["raw_output_chars"] > audit["summary_output_chars"]
    assert audit["summary_output_chars"] == len(compact_result)

    tool_item = next(item for item in agent.session["history"] if item["role"] == "tool")
    assert "FAILED tests/test_checkout.py::test_cross_module_total" in tool_item["summary"]
    artifact = (agent.current_run_dir / tool_item["content_ref"]).read_text(encoding="utf-8")
    assert "noise line 0000" in artifact
    assert "FAILED tests/test_checkout.py::test_cross_module_total" in artifact
    assert len(artifact) == audit["raw_output_chars"]


def test_short_pytest_failure_is_prioritized_in_history_summary(tmp_path):
    agent = build_agent(tmp_path, [])
    result = (
        "sandbox: test\n"
        "exit_code: 1\n"
        "stdout:\n"
        "collected 8 items\n"
        ".......F\n"
        "FAILED tests/test_checkout.py::test_total - AssertionError\n"
        "1 failed, 7 passed in 0.42s\n"
        "stderr:\n"
        "(empty)"
    )

    summary = agent.summarize_tool_result(
        "run_shell",
        {"command": "pytest -q"},
        result,
    )

    assert "FAILED tests/test_checkout.py::test_total" in summary
    assert "1 failed, 7 passed in 0.42s" in summary


def test_stagnation_nudge_is_injected_once_after_three_unchanged_calls(tmp_path):
    class NativeModelClient:
        supports_native_actions = True
        supports_prompt_cache = False

        def __init__(self):
            self.results = []
            self.last_completion_metadata = {}
            self.actions = [
                *[
                    ModelAction.tool(
                        "list_files",
                        {"path": "."},
                        protocol="responses_function",
                        call_id=f"call_{index}",
                    )
                    for index in range(1, 5)
                ],
                ModelAction.final("Changed approach.", protocol="responses_function", call_id="call_5"),
            ]

        def reset_action_session(self):
            self.results = []

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [], max_steps=5)
    client = NativeModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect the workspace.") == "Changed approach."

    nudged_results = [result for _, result in client.results if "progress_nudge:" in result]
    assert len(nudged_results) == 1
    assert [entry["status"] for entry in agent.tool_audit_log] == ["ok"] * 4

    trace_records = [
        json.loads(line)
        for line in (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    nudge_events = [record for record in trace_records if record["event"] == "progress_nudge"]
    assert len(nudge_events) == 1
    assert nudge_events[0]["tool_name"] == "list_files"
    assert nudge_events[0]["repeat_count"] == 3


def test_text_protocol_receives_stagnation_nudge_in_next_prompt(tmp_path):
    repeated_call = '<tool>{"name":"list_files","args":{"path":"."}}</tool>'
    agent = build_agent(
        tmp_path,
        [repeated_call, repeated_call, repeated_call, "<final>Changed approach.</final>"],
        max_steps=4,
    )

    assert agent.ask("Inspect the workspace.") == "Changed approach."

    assert "progress_nudge:" in agent.model_client.prompts[3]
    nudge_history = [
        item
        for item in agent.session["history"]
        if item["role"] == "assistant" and "progress_nudge:" in item["content"]
    ]
    assert len(nudge_history) == 1


def test_workspace_change_breaks_identical_call_stagnation_streak(tmp_path):
    repeated_write = (
        '<tool>{"name":"write_file","args":{"path":"same.txt","content":"same"}}</tool>'
    )
    agent = build_agent(
        tmp_path,
        [repeated_write, repeated_write, repeated_write, "<final>Finished.</final>"],
        max_steps=4,
    )

    assert agent.ask("Write the file.") == "Finished."

    assert [entry["workspace_changed"] for entry in agent.tool_audit_log] == [
        True,
        False,
        False,
    ]
    assert all("progress_nudge:" not in prompt for prompt in agent.model_client.prompts)


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
    assert (
        "When a task graph node has a ref, use read_tool_output instead of manually reading tool_outputs paths."
        in prompt
    )


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

    process_notes = [
        note["text"]
        for note in agent.session["memory"]["episodic_notes"]
        if note["kind"] == "process"
    ]
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

    notices = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
    assert any("empty response" in item for item in notices)


def test_agent_records_stopped_state_when_step_limit_is_reached(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>'
        ],
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


def test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling(
    tmp_path,
):
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
    notices = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
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
    notices = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
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
    notices = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
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
    notices = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
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
    assert any(
        item["role"] == "tool" and item["name"] == "read_file"
        for item in agent.session["history"]
    )
    notices = [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
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
