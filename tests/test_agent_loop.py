"""Interview-focused tests for the bounded structured-action loop."""

import json

from pico.actions import ModelAction
from pico.sandbox import SandboxResult
from tests.fakes import final_action, retry_action, tool_action_json
from tests.helpers import UnitTestSandbox, build_agent


def test_agent_stops_at_the_exact_retry_limit(tmp_path):
    malformed = retry_action("function read_file returned malformed JSON arguments")
    agent = build_agent(tmp_path, [malformed] * 5, max_steps=1)

    answer = agent.ask("Keep returning malformed actions")

    assert answer.startswith("Stopped after too many rejected model actions")
    assert agent.current_task_state.attempts == 5
    assert agent.current_task_state.stop_reason == "retry_limit_reached"


def test_agent_runs_tool_then_final_and_records_the_task_graph(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            tool_action_json(
                '{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}'
            ),
            final_action("Read the file successfully."),
        ],
    )

    assert agent.ask("Inspect hello.txt") == "Read the file successfully."

    tool_item = next(
        item
        for item in agent.session["history"]
        if item["role"] == "tool" and item["name"] == "read_file"
    )
    assert tool_item["node_id"] == "t001_read_file"
    assert tool_item["content_ref"].endswith("tool_outputs/0001_read_file.txt")
    graph = (agent.current_run_dir / "task_graph.mmd").read_text(encoding="utf-8")
    assert 't001_read_file["tool | ok | read_file hello.txt"]' in graph
    assert "tool_outputs/0001_read_file.txt" in graph


def test_strict_action_loop_reuses_the_structured_conversation_prompt(tmp_path):
    class StrictModelClient:
        model = "strict-test"
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
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect README.md") == "Finished."
    assert client.prompts[0] == client.prompts[1]
    assert client.results[0][0] == "call_1"
    assert agent.last_prompt_metadata["prompt_reused"] is True


def test_pytest_failure_tail_stays_in_context_and_full_output_stays_on_disk(tmp_path):
    class LongPytestSandbox(UnitTestSandbox):
        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout, env
            noise = "".join(f"noise line {index:04d}\n" for index in range(800))
            return SandboxResult(
                returncode=1,
                stdout=(
                    f"{noise}"
                    "FAILED tests/test_checkout.py::test_cross_module_total - AssertionError\n"
                    "1 failed, 7 passed in 0.42s\n"
                ),
            )

    class StrictModelClient:
        model = "strict-test"
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
                ModelAction.final(
                    "Finished.",
                    protocol="responses_function",
                    call_id="call_2",
                ),
            ]

        def reset_action_session(self):
            self.results = []

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [], sandbox=LongPytestSandbox(tmp_path))
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Run pytest and inspect the failure.") == "Finished."

    compact_result = client.results[0][1]
    assert len(compact_result) <= 4000
    assert "FAILED tests/test_checkout.py::test_cross_module_total" in compact_result
    assert "1 failed, 7 passed in 0.42s" in compact_result
    tool_item = next(item for item in agent.session["history"] if item["role"] == "tool")
    artifact = (agent.current_run_dir / tool_item["content_ref"]).read_text(
        encoding="utf-8"
    )
    assert "noise line 0000" in artifact
    assert len(artifact) == agent.tool_audit_log[0]["raw_output_chars"]


def test_stagnation_nudge_is_injected_once_after_three_unchanged_calls(tmp_path):
    class StrictModelClient:
        model = "strict-test"
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
                ModelAction.final(
                    "Changed approach.",
                    protocol="responses_function",
                    call_id="call_5",
                ),
            ]

        def reset_action_session(self):
            self.results = []

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [], max_steps=5)
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect the workspace.") == "Changed approach."
    assert sum("progress_nudge:" in result for _, result in client.results) == 1
    trace_records = [
        json.loads(line)
        for line in (agent.current_run_dir / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sum(record["event"] == "progress_nudge" for record in trace_records) == 1


def test_tool_limit_allows_one_final_only_turn(tmp_path):
    class StrictModelClient:
        model = "strict-test"
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
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect once, then finish") == "Finished at the tool limit."
    assert "list_files" in client.tool_sets[0]
    assert client.tool_sets[1] == ["submit_final"]
    assert agent.current_task_state.tool_steps == 1


def test_final_only_turn_cannot_execute_another_tool(tmp_path):
    class StrictModelClient:
        model = "strict-test"
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
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    answer = agent.ask("Try to exceed the tool budget")

    assert answer == "Stopped after reaching the step limit without a final answer."
    assert client.tool_sets[1] == ["submit_final"]
    assert not (tmp_path / "too-late.txt").exists()


def test_agent_recovers_from_malformed_tool_payload_and_audits_it(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            retry_action(
                "function read_file returned malformed JSON arguments",
                raw_preview='{"name":"read_file","args":"bad"}',
            ),
            tool_action_json(
                '{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}'
            ),
            final_action("Recovered."),
        ],
    )

    assert agent.ask("Inspect hello.txt") == "Recovered."
    assert len(agent.model_action_rejections) == 1
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["summary"]["model_action_rejection_count"] == 1
    assert report["model_action_rejections"][0]["raw_preview"].startswith("{")
