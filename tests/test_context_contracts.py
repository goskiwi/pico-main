import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import AssistantTurn, ModelAction, ToolCall, ToolOutcome
from pico.compaction_summary import CompactedContext
from pico.prompt_builder import load_project_instructions
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
    def test_runtime_permissions_live_in_the_system_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(Path(directory), [])
            RunLifecycle(agent).initialize("Inspect")

            prompt = agent.prompt.build_for_run(
                tool_surface=agent.tools.resolve_surface()
            )

            self.assertIn("Run permissions", prompt.system_prompt)
            self.assertIn('"mode": "auto"', prompt.system_prompt)
            self.assertFalse(
                any("Run permissions" in message.text for message in prompt.messages)
            )
            self.assertFalse(hasattr(prompt, "input_text"))

    def test_failure_correction_is_the_last_developer_message(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(Path(directory), [])
            RunLifecycle(agent).initialize("Inspect")
            agent.run.run_log.append("model_requested")
            agent.run.run_log.append_model_failure(
                "protocol_error",
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
                agent.tools,
                "resolve_surface",
                wraps=agent.tools.resolve_surface,
            ) as surface:
                outcome = agent.ask("Read subject.txt")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(history.call_count, 1)
            self.assertEqual(surface.call_count, 1)


if __name__ == "__main__":
    unittest.main()
