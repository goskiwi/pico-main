import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pico import AssistantTurn, ModelAction, PicoConfig, ToolCall, ToolOutcome
from pico.compaction_summary import (
    SUMMARY_TOOL,
    CompactedContext,
    CompactionSummarizer,
    SemanticCompactionError,
)
from pico.execution import ExecutionContext
from pico.prompt_builder import load_project_instructions
from pico.providers import ProviderContextOverflow
from pico.run_lifecycle import RunLifecycle
from tests.support import build_agent, request_text


class RepositoryInstructionTests(unittest.TestCase):
    def test_root_rules_refresh_before_next_request_including_final(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("ROOT_RULE_V1\n", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "AGENTS.md").write_text("NESTED_RULE\n", encoding="utf-8")
            (nested / "subject.txt").write_text("alpha\n", encoding="utf-8")

            def update_rule_during_first_request(index):
                if index == 0:
                    (root / "AGENTS.md").write_text(
                        "ROOT_RULE_V2\n", encoding="utf-8"
                    )

            agent, model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "nested/subject.txt"}),
                    ModelAction.final("Read with current rules."),
                ],
                before_action=update_rule_during_first_request,
            )

            with mock.patch(
                "pico.prompt_builder.load_project_instructions",
                wraps=load_project_instructions,
            ) as load:
                outcome = agent.ask("Inspect the nested subject")

            self.assertEqual(outcome.status, "completed")
            self.assertIn("ROOT_RULE_V1", request_text(model.requests[0]))
            self.assertNotIn("ROOT_RULE_V2", request_text(model.requests[0]))
            self.assertIn("ROOT_RULE_V2", request_text(model.requests[1]))
            self.assertTrue(
                all("NESTED_RULE" not in request_text(request) for request in model.requests)
            )
            self.assertEqual(load.call_count, 2)
            resets = [
                event
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "provider_session_reset"
            ]
            self.assertEqual(len(resets), 1)
            self.assertEqual(
                resets[0].payload["reason"],
                "project_instructions_changed",
            )


class ModelMessageTests(unittest.TestCase):
    def test_summary_input_only_truncates_tool_result_content(self):
        user_text = "U" * 3_000
        argument_text = "A" * 3_000
        outcome = ToolOutcome(
            "read",
            "read_file",
            "success",
            "completed",
            "none",
            "X" * 3_000,
            structured={"path": "subject.txt"},
            artifact_id="tool_0123456789abcdef_0123456789",
        )
        user = SimpleNamespace(
                kind="user_guidance",
                payload={"content": user_text},
            )
        call = SimpleNamespace(
                kind="tool_call",
                payload={
                    "name": "edit_file",
                    "args": {
                        "path": "subject.txt",
                        "new_text": argument_text,
                    },
                },
            )
        result = SimpleNamespace(
                kind="tool_result",
                payload={"outcome": outcome.to_dict()},
            )
        groups = ((user,), (call, result))

        rendered, dropped = CompactionSummarizer._summary_input(
            groups,
            count_tokens=len,
            input_budget=20_000,
        )

        self.assertEqual(dropped, 0)
        self.assertIn(user_text, rendered)
        self.assertIn(argument_text, rendered)
        self.assertIn("X" * 2_000, rendered)
        self.assertNotIn("X" * 2_001, rendered)
        self.assertIn("full retained output: artifact_id=tool_", rendered)
        self.assertNotIn("RunLog", rendered)

    def test_required_summary_content_over_budget_fails(self):
        event = SimpleNamespace(
            kind="user_guidance",
            payload={"content": "required" * 1_000},
        )

        with self.assertRaisesRegex(
            SemanticCompactionError,
            "exceeds the summary input budget",
        ):
            CompactionSummarizer._summary_input(
                ((event,),),
                count_tokens=len,
                input_budget=100,
            )

    def test_summary_input_omits_oldest_complete_tool_turn_first(self):
        user = SimpleNamespace(
            kind="user_guidance",
            payload={"content": "Keep this requirement"},
        )

        def tool_group(call_id, path, content):
            call = SimpleNamespace(
                kind="tool_call",
                payload={
                    "name": "read_file",
                    "args": {"path": path},
                },
            )
            outcome = ToolOutcome(
                call_id,
                "read_file",
                "success",
                "completed",
                "none",
                content,
                structured={"path": path},
            )
            result = SimpleNamespace(
                kind="tool_result",
                payload={"outcome": outcome.to_dict()},
            )
            return call, result

        rendered, dropped = CompactionSummarizer._summary_input(
            (
                (user,),
                tool_group("old", "old.py", "X" * 3_000),
                tool_group("recent", "recent.py", "ok"),
            ),
            count_tokens=len,
            input_budget=1_000,
        )

        self.assertEqual(dropped, 1)
        self.assertIn("Keep this requirement", rendered)
        self.assertNotIn("old.py", rendered)
        self.assertIn("recent.py", rendered)
        self.assertIn("1 older complete Tool Turns were omitted", rendered)

    def test_summary_provider_overflow_omits_one_more_turn_and_retries(self):
        call = SimpleNamespace(
            kind="tool_call",
            payload={"name": "read_file", "args": {"path": "old.py"}},
        )
        outcome = ToolOutcome(
            "old",
            "read_file",
            "success",
            "completed",
            "none",
            "old result",
        )
        result = SimpleNamespace(
            kind="tool_result",
            payload={"outcome": outcome.to_dict()},
        )
        summary_args = {
            "constraints": [],
            "progress": {"done": [], "in_progress": [], "blocked": []},
            "key_decisions": [],
            "next_steps": [],
            "critical_context": [],
        }
        client = mock.Mock()
        client.complete_turn.side_effect = [
            ProviderContextOverflow("too large"),
            AssistantTurn(
                ModelAction.tool(
                    SUMMARY_TOOL["name"],
                    summary_args,
                    call_id="summary",
                )
            ),
        ]
        summarizer = CompactionSummarizer(lambda: client)

        compacted = summarizer.summarize(
            ((call, result),),
            task_goal="Inspect",
            execution_context=ExecutionContext.root(max_seconds=5),
            effective_context_limit_tokens=100_000,
            effective_input_limit_tokens=100_000,
            max_output_tokens=1_000,
            count_tokens=len,
        )

        self.assertIsInstance(compacted, CompactedContext)
        self.assertEqual(client.complete_turn.call_count, 2)
        client.reset_action_session.assert_called_once_with()
        second_messages = client.complete_turn.call_args_list[1].args[0]
        self.assertIn("1 older complete Tool Turns were omitted", second_messages[0].text)
        client.close.assert_called_once_with()

    def test_runtime_permissions_live_in_the_system_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(Path(directory), [])
            RunLifecycle(agent).initialize("Inspect")

            prompt = agent.prompt.build_for_run(
                tool_surface=agent.tools.resolve_surface()
            )

            self.assertIn("Run permissions", prompt.system_prompt)
            self.assertIn("- Mode: auto.", prompt.system_prompt)
            self.assertIn(
                "shell commands still require user approval",
                prompt.system_prompt,
            )
            self.assertFalse(
                any("Run permissions" in message.text for message in prompt.messages)
            )
            self.assertFalse(hasattr(prompt, "input_text"))

    def test_path_limited_code_permissions_are_plain_language(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(Path(directory), [])
            agent.config = PicoConfig(
                mode="code",
                allowed_write_paths=("src/cart.py",),
                context_limit_tokens=32_000,
                max_output_tokens=1_000,
                recent_history_tokens=2_000,
            )
            RunLifecycle(agent).initialize("Fix cart.py")

            prompt = agent.prompt.build_for_run(
                tool_surface=agent.tools.resolve_surface()
            )

            self.assertIn("- Mode: code.", prompt.system_prompt)
            self.assertIn(
                "- You may modify only: src/cart.py.",
                prompt.system_prompt,
            )
            self.assertIn(
                "File modifications and shell commands require user approval.",
                prompt.system_prompt,
            )
            self.assertNotIn("write_scope", prompt.system_prompt)

    def test_failure_correction_is_the_last_developer_message(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(Path(directory), [])
            RunLifecycle(agent).initialize("Inspect")
            agent.run.run_log.append("model_requested")
            agent.run.run_log.append_model_failure(
                "invalid",
                "malformed response",
                "malformed response",
                {},
            )

            prompt = agent.prompt.build_for_run(
                tool_surface=agent.tools.resolve_surface()
            )

            self.assertNotIn("previous model response", prompt.system_prompt.lower())
            self.assertEqual(prompt.messages[-1].role, "developer")
            self.assertIn(
                "previous model response",
                prompt.messages[-1].text.lower(),
            )

    def test_user_assistant_and_tool_messages_preserve_chronology(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(root, [])
            RunLifecycle(agent).initialize("Inspect subject.txt")
            call = ToolCall("read_file", {"path": "subject.txt"}, "read")
            log = agent.run.run_log
            log.append("model_requested")
            log.append_assistant_turn(
                AssistantTurn(
                    ModelAction.tool(call.name, call.args, call_id=call.call_id),
                    "I will inspect it.",
                )
            )
            log.append_tool_result(
                ToolOutcome(
                    "read",
                    "read_file",
                    "success",
                    "completed",
                    "none",
                    "alpha",
                )
            )
            log.append_user_guidance("Do not change the public API")

            prompt = agent.prompt.build_for_run(
                tool_surface=agent.tools.resolve_surface()
            )

            messages = prompt.messages
            start = next(
                index
                for index, message in enumerate(messages)
                if message.role == "user" and message.text == "Inspect subject.txt"
            )
            self.assertEqual(
                [message.role for message in messages[start:]],
                ["user", "assistant", "tool", "user"],
            )
            self.assertEqual(messages[start + 1].tool_calls, (call,))
            self.assertEqual(messages[start + 2].tool_call_id, "read")
            self.assertEqual(
                messages[start + 3].text,
                "Do not change the public API",
            )

    def test_committed_summary_precedes_recent_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(Path(directory), [])
            RunLifecycle(agent).initialize("Inspect")
            old = agent.run.run_log.append_user_guidance("older guidance")
            agent.run.run_log.append_compaction(
                CompactedContext(
                    constraints=("Keep the API",),
                    progress_done=(),
                    progress_in_progress=(),
                    progress_blocked=(),
                    key_decisions=(),
                    next_steps=("Continue inspection",),
                    critical_context=(),
                    covered_through_sequence=old.sequence,
                )
            )
            agent.run.run_log.append_user_guidance("new guidance")

            prompt = agent.prompt.build_for_run(
                tool_surface=agent.tools.resolve_surface()
            )
            texts = [message.text for message in prompt.messages]

            summary = next(
                index
                for index, text in enumerate(texts)
                if text.startswith("<conversation_summary>")
            )
            self.assertEqual(texts[summary + 1], "new guidance")
            self.assertNotIn("older guidance", texts)

    def test_one_prompt_rebuild_reuses_one_history_snapshot_and_tool_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.final("Read."),
                ],
            )

            with mock.patch.object(
                agent.prompt,
                "_history",
                wraps=agent.prompt._history,
            ) as history, mock.patch.object(
                agent.prompt,
                "_messages",
                wraps=agent.prompt._messages,
            ) as messages, mock.patch.object(
                agent.tools,
                "resolve_surface",
                wraps=agent.tools.resolve_surface,
            ) as surface:
                outcome = agent.ask("Read subject.txt")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(history.call_count, 1)
            self.assertEqual(messages.call_count, 1)
            self.assertEqual(surface.call_count, 1)


if __name__ == "__main__":
    unittest.main()
