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


def test_agent_runs_tool_then_final_and_records_the_task_canvas(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            tool_action_json(
                '{"name":"read_file","args":{"files":[{"path":"hello.txt","start":1,"end":2}]}}'
            ),
            final_action("Read the file successfully."),
        ],
    )

    assert agent.ask("Inspect hello.txt") == "Read the file successfully."

    canvas = (agent.current_run_dir / "task.mmd").read_text(encoding="utf-8")
    assert 'N001_read_file["task_step | done | read_file hello.txt:' in canvas
    assert "refs/0001_read_file.txt" in canvas
    event = json.loads((agent.current_run_dir / "offload.jsonl").read_text(encoding="utf-8"))
    assert event["node_id"] == "N001_read_file"
    assert event["result_ref"].endswith("refs/0001_read_file.txt")


def test_agent_automatically_folds_long_task_canvas_into_phase_artifacts(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            *[
                tool_action_json('{"name":"list_files","args":{"path":"."}}')
                for _ in range(13)
            ],
            final_action("Finished the long inspection."),
        ],
        max_steps=14,
    )

    assert agent.ask("Inspect the workspace in depth.") == "Finished the long inspection."

    run_dir = agent.current_run_dir
    active_canvas = (run_dir / "task.mmd").read_text(encoding="utf-8")
    phase_canvas = (run_dir / "phases" / "phase_001.mmd").read_text(encoding="utf-8")
    assert 'A["archive | attention | 1 phases / 5 task steps' in active_canvas
    assert "N001_list_files" not in active_canvas
    assert "N006_list_files" in active_canvas
    assert "N001_list_files" in phase_canvas


def test_strict_action_loop_keeps_live_tool_conversation_after_each_tool(tmp_path):
    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.prompts = []
            self.results = []
            self.reset_count = 0
            self.last_completion_metadata = {}
            self.actions = [
                ModelAction.tool(
                    "read_file",
                    {"files": [{"path": "README.md", "start": 1, "end": 1}]},
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
            self.reset_count += 1

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
    assert "Task state" in client.prompts[0]
    assert "N001_read_file" not in client.prompts[1]
    assert client.results[0][0] == "call_1"
    assert "README.md" in client.results[0][1]
    assert client.reset_count == 1
    assert agent.last_prompt_metadata["prompt_reused"] is True


def test_provider_context_limit_recovers_once_with_recent_tool_evidence(tmp_path):
    class ContextLimitClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.prompts = []
            self.results = []
            self.reset_count = 0
            self.calls = 0
            self.last_completion_metadata = {}

        def reset_action_session(self):
            self.reset_count += 1

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del max_new_tokens, kwargs
            self.prompts.append(prompt)
            self.calls += 1
            if self.calls == 1:
                return ModelAction.tool(
                    "read_file",
                    {"files": [{"path": "README.md", "start": 1, "end": 2}]},
                    protocol="responses_function",
                    call_id="call_1",
                )
            if self.calls == 2:
                raise RuntimeError("maximum context length exceeded")
            return ModelAction.final(
                "Recovered from the provider context limit.",
                protocol="responses_function",
                call_id="call_2",
            )

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [])
    (tmp_path / "README.md").write_text("alpha\nbeta\n", encoding="utf-8")
    client = ContextLimitClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect README.md") == "Recovered from the provider context limit."

    assert client.calls == 3
    assert client.reset_count == 2  # task start + one recovery, never a loop
    assert "Context recovery after a provider context-limit error:" in client.prompts[2]
    assert "Latest tool evidence N001_read_file" in client.prompts[2]
    assert "Fresh workspace snapshot | README.md:" in client.prompts[2]
    assert "alpha\nbeta" in client.prompts[2]
    trace = (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"event": "context_recovery_started"' in trace
    assert '"event": "context_recovery_completed"' in trace


def test_second_provider_context_limit_stops_instead_of_repeatedly_compacting(tmp_path):
    class AlwaysContextLimitedClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.calls = 0
            self.reset_count = 0
            self.last_completion_metadata = {}

        def reset_action_session(self):
            self.reset_count += 1

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            self.calls += 1
            raise RuntimeError("input is too long for this context window")

        def record_action_result(self, action, result):
            del action, result

    agent = build_agent(tmp_path, [])
    client = AlwaysContextLimitedClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    answer = agent.ask("Inspect the workspace.")

    assert answer.startswith("Stopped after model error: RuntimeError: input is too long")
    assert client.calls == 2
    assert client.reset_count == 2  # task start + one recovery only
    trace = (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert trace.count('"event": "context_recovery_started"') == 1
    assert '"event": "context_recovery_completed"' not in trace


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

    event = json.loads((agent.current_run_dir / "offload.jsonl").read_text(encoding="utf-8"))
    assert event["node_id"] == "N001_run_shell"
    assert "FAILED tests/test_checkout.py::test_cross_module_total" in event["summary"]
    assert "1 failed, 7 passed in 0.42s" in event["summary"]
    artifact = (agent.current_run_dir / event["result_ref"]).read_text(
        encoding="utf-8"
    )
    assert "noise line 0000" in artifact
    assert len(artifact) == agent.tool_audit_log[0]["raw_output_chars"]


def test_identical_read_only_tool_is_rejected_after_first_call(tmp_path):
    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.results = []
            self.last_completion_metadata = {}
            self.actions = [
                ModelAction.tool(
                    "list_files",
                    {"path": "."},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.tool(
                    "list_files",
                    {"path": "."},
                    protocol="responses_function",
                    call_id="call_2",
                ),
                ModelAction.final(
                    "Changed approach.",
                    protocol="responses_function",
                    call_id="call_3",
                ),
            ]

        def reset_action_session(self):
            self.results = []

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [], max_steps=3)
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Inspect the workspace.") == "Changed approach."
    assert [item["status"] for item in agent.tool_audit_log] == ["ok", "rejected"]
    assert agent.tool_audit_log[1]["error_code"] == "duplicate_read_only_call"
    assert "duplicate read-only call blocked" in agent.tool_audit_log[1]["result_preview"]
    assert "README.md" in client.results[1][1]
    assert "[F] README.md" in client.results[1][1]


def test_identical_read_only_tool_is_allowed_after_workspace_change(tmp_path):
    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.results = []
            self.last_completion_metadata = {}
            self.actions = [
                ModelAction.tool(
                    "read_file",
                    {"files": [{"path": "README.md"}]},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.tool(
                    "write_file",
                    {"path": "README.md", "content": "updated\\n"},
                    protocol="responses_function",
                    call_id="call_2",
                ),
                ModelAction.tool(
                    "read_file",
                    {"files": [{"path": "README.md"}]},
                    protocol="responses_function",
                    call_id="call_3",
                ),
                ModelAction.final(
                    "Updated and re-read.",
                    protocol="responses_function",
                    call_id="call_4",
                ),
            ]

        def reset_action_session(self):
            self.results = []

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    (tmp_path / "README.md").write_text("original\\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], max_steps=4)
    agent.model_client = StrictModelClient()
    agent.refresh_prefix(force=True)

    assert agent.ask("Update the README.") == "Updated and re-read."
    assert [item["status"] for item in agent.tool_audit_log] == ["ok", "ok", "ok"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "updated\\n"


def test_patch_conflict_returns_current_file_as_repair_evidence(tmp_path):
    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}
            self.actions = [
                ModelAction.tool(
                    "patch_file",
                    {
                        "path": "README.md",
                        "old_text": "stale\n",
                        "new_text": "replacement\n",
                    },
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.final("The patch needs repair.", protocol="responses_function"),
            ]

        def reset_action_session(self):
            pass

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            del action, result

    agent = build_agent(tmp_path, [])
    agent.model_client = StrictModelClient()
    agent.refresh_prefix(force=True)

    assert agent.ask("Replace stale README text.") == "The patch needs repair."
    audit = agent.tool_audit_log[0]
    assert audit["status"] == "rejected"
    assert audit["error_code"] == "patch_conflict"
    assert "current file" in audit["result_preview"]
    event = json.loads((agent.current_run_dir / "offload.jsonl").read_text(encoding="utf-8"))
    evidence = (agent.current_run_dir / event["result_ref"]).read_text(encoding="utf-8")
    assert "demo" in evidence


def test_pytest_pipeline_failure_cannot_be_masked_by_a_zero_shell_exit(tmp_path):
    class MaskingSandbox(UnitTestSandbox):
        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout, env
            return SandboxResult(
                returncode=0,
                stdout="FAILED tests/test_checkout.py::test_total\n1 failed, 2 passed in 0.01s\n",
            )

    agent = build_agent(tmp_path, [], sandbox=MaskingSandbox(tmp_path))
    result = agent.run_tool("run_shell", {"command": "pytest -q | tail -n 20", "timeout": 20})

    assert "1 failed" in result
    assert agent._last_tool_result_metadata["tool_status"] == "error"
    assert agent._last_tool_result_metadata["tool_error_code"] == "pytest_failed"
    assert agent._last_tool_result_metadata["verification"] == {
        "framework": "pytest",
        "passed": False,
        "exit_code": 0,
        "failed": 1,
        "errors": 0,
        "pipeline_masked_failure": True,
    }


def test_masked_pytest_failure_rejects_a_false_final_until_the_task_is_unverified(tmp_path):
    class MaskingSandbox(UnitTestSandbox):
        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout, env
            return SandboxResult(
                returncode=0,
                stdout="FAILED tests/test_checkout.py::test_total\n1 failed, 2 passed in 0.01s\n",
            )

    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}
            self.results = []
            self.actions = [
                ModelAction.tool(
                    "write_file",
                    {"path": "changed.txt", "content": "first attempt\n"},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.tool(
                    "run_shell",
                    {"command": "pytest -q | tail -n 20", "timeout": 20},
                    protocol="responses_function",
                    call_id="call_2",
                ),
                ModelAction.final(
                    "The tests passed.",
                    protocol="responses_function",
                    call_id="call_3",
                ),
                ModelAction.tool(
                    "write_file",
                    {"path": "changed.txt", "content": "repair pending verification\n"},
                    protocol="responses_function",
                    call_id="call_4",
                ),
                ModelAction.final(
                    "Applied a repair, but pytest remains unverified.",
                    protocol="responses_function",
                    call_id="call_5",
                ),
            ]

        def reset_action_session(self):
            pass

        def complete_action(self, prompt, max_new_tokens, **kwargs):
            del prompt, max_new_tokens, kwargs
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            self.results.append((action.call_id, result))

    agent = build_agent(tmp_path, [], sandbox=MaskingSandbox(tmp_path), max_steps=5)
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    answer = agent.ask("Change the workspace and run pytest.")

    assert answer == "Applied a repair, but pytest remains unverified."
    assert agent.tool_audit_log[1]["status"] == "error"
    assert agent.tool_audit_log[1]["error_code"] == "pytest_failed"
    assert any(
        "latest pytest verification failed" in rejection["reason"]
        for rejection in agent.model_action_rejections
    )
    assert (tmp_path / "changed.txt").read_text(encoding="utf-8") == "repair pending verification\n"


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

    agent = build_agent(tmp_path, [], max_steps=1, max_step_extension=0)
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

    agent = build_agent(tmp_path, [], max_steps=1, max_step_extension=0)
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    answer = agent.ask("Try to exceed the tool budget")

    assert answer == "Stopped after reaching the step limit without a final answer."
    assert client.tool_sets[1] == ["submit_final"]
    assert not (tmp_path / "too-late.txt").exists()


def test_nominal_budget_is_soft_while_hard_limit_still_bounds_the_task(tmp_path):
    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}
            self.tool_sets = []
            self.actions = [
                ModelAction.tool(
                    "write_file",
                    {"path": "changed.txt", "content": "ready\n"},
                    protocol="responses_function",
                    call_id="call_1",
                ),
                ModelAction.tool(
                    "list_files",
                    {"path": "."},
                    protocol="responses_function",
                    call_id="call_2",
                ),
                ModelAction.final(
                    "Changed the file and completed the bounded follow-up.",
                    protocol="responses_function",
                    call_id="call_3",
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

    agent = build_agent(tmp_path, [], max_steps=1, max_step_extension=1)
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Make a small change and verify it.").startswith("Changed the file")
    assert "list_files" in client.tool_sets[1]
    assert client.tool_sets[2] == ["submit_final"]
    assert agent.current_task_state.nominal_tool_budget == 1
    assert agent.current_task_state.hard_tool_limit == 2
    assert agent.current_task_state.tool_steps == 2


def test_soft_budget_does_not_force_finalization_after_an_old_workspace_change(tmp_path):
    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}
            self.tool_sets = []
            self.actions = [
                ModelAction.tool(
                    "write_file",
                    {"path": "changed.txt", "content": "ready\n"},
                    protocol="responses_function",
                ),
                *[
                    ModelAction.tool(
                        "list_files",
                        {"path": "."},
                        protocol="responses_function",
                    )
                    for _ in range(4)
                ],
                ModelAction.final("No more useful progress remained.", protocol="responses_function"),
            ]

        def reset_action_session(self):
            pass

        def complete_action(self, prompt, max_new_tokens, *, action_tools, **kwargs):
            del prompt, max_new_tokens, kwargs
            self.tool_sets.append([tool["name"] for tool in action_tools])
            return self.actions.pop(0)

        def record_action_result(self, action, result):
            del action, result

    agent = build_agent(tmp_path, [], max_steps=5, max_step_extension=5)
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    assert agent.ask("Avoid looping after an old edit.").startswith("No more useful progress")
    assert "list_files" in client.tool_sets[-1]


def test_successful_pytest_after_a_change_is_finalized_without_another_model_call(tmp_path):
    class PassingPytestSandbox(UnitTestSandbox):
        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout, env
            return SandboxResult(returncode=0, stdout="1 passed in 0.01s\n")

    class StrictModelClient:
        model = "strict-test"
        supports_prompt_cache = False

        def __init__(self):
            self.last_completion_metadata = {}
            self.tool_sets = []
            self.actions = [
                ModelAction.tool(
                    "write_file",
                    {"path": "changed.txt", "content": "ready\n"},
                    protocol="responses_function",
                ),
                ModelAction.tool(
                    "run_shell",
                    {"command": "pytest -q", "timeout": 20},
                    protocol="responses_function",
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

    agent = build_agent(tmp_path, [], max_steps=6, sandbox=PassingPytestSandbox(tmp_path))
    client = StrictModelClient()
    agent.model_client = client
    agent.refresh_prefix(force=True)

    answer = agent.ask("Change the workspace and run pytest.")

    assert answer == (
        "Completed verified changes.\n"
        "Changed files: changed.txt\n"
        "Verification: pytest -q — pytest passed."
    )
    assert len(client.tool_sets) == 2
    assert agent.current_task_state.stop_reason == "final_answer_returned"
    trace = (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"event": "runtime_finalized"' in trace


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
                '{"name":"read_file","args":{"files":[{"path":"hello.txt","start":1,"end":1}]}}'
            ),
            final_action("Recovered."),
        ],
    )

    assert agent.ask("Inspect hello.txt") == "Recovered."
    assert len(agent.model_action_rejections) == 1
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["summary"]["model_action_rejection_count"] == 1
    assert report["model_action_rejections"][0]["raw_preview"].startswith("{")
