import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import ModelAction, ModelMessage, ToolCall
from pico.compaction_summary import SUMMARY_TOOL
from pico.execution import ExecutionContext
from pico.providers.clients import (
    OpenAICompatibleModelClient,
    ProviderContextOverflow,
    _message_items,
    _parse_turn,
    _replay_context_tokens,
    _result_items,
)
from pico.run_log import replay_events
from tests.support import build_agent


class ModelResultTests(unittest.TestCase):
    @staticmethod
    def _tools():
        return (
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "submit_final"},
        )

    def test_truncated_response_never_becomes_an_executable_call(self):
        turn = _parse_turn(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "function_call",
                        "name": "write_file",
                        "call_id": "cut",
                        "arguments": '{"path":"subject.txt","content":"partial',
                    }
                ],
            },
            self._tools(),
        )

        self.assertEqual(turn.action.kind, "truncated")
        self.assertEqual(turn.action.tool_calls, ())
        self.assertFalse(turn.accepted)

    def test_service_and_protocol_failures_are_distinct(self):
        service = _parse_turn(
            {
                "status": "failed",
                "error": {"message": "upstream unavailable"},
            },
            self._tools(),
        )
        protocol = _parse_turn(
            {"status": "completed", "output": []},
            self._tools(),
        )

        self.assertEqual(service.action.kind, "service_failed")
        self.assertEqual(protocol.action.kind, "protocol_error")

    def test_provider_failure_does_not_reuse_previous_request_usage(self):
        client = OpenAICompatibleModelClient(
            "test-model",
            "https://example.invalid/v1",
            "test-key",
            None,
            1,
        )
        client.last_completion_metadata = {"input_tokens": 99}
        try:
            with mock.patch.object(
                client,
                "_request",
                side_effect=ProviderContextOverflow("too large"),
            ), self.assertRaises(ProviderContextOverflow):
                client.complete_turn(
                    (ModelMessage.user("Inspect"),),
                    100,
                    system_prompt="test",
                    action_tools=self._tools(),
                    execution_context=ExecutionContext.root(max_seconds=1),
                )
            self.assertEqual(client.last_completion_metadata, {})
        finally:
            client.close()

    def test_multiple_tool_calls_preserve_order_and_result_identity(self):
        turn = _parse_turn(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "read-a",
                        "arguments": '{"path":"a.py"}',
                    },
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "read-b",
                        "arguments": '{"path":"b.py"}',
                    },
                ],
            },
            self._tools(),
        )

        self.assertEqual(
            [(call.call_id, call.name) for call in turn.action.tool_calls],
            [("read-a", "read_file"), ("read-b", "read_file")],
        )
        self.assertEqual(turn.pending_call_ids, ("read-a", "read-b"))
        self.assertEqual(
            _result_items(turn.pending_call_ids, ("result-a", "result-b")),
            [
                {
                    "type": "function_call_output",
                    "call_id": "read-a",
                    "output": "result-a",
                },
                {
                    "type": "function_call_output",
                    "call_id": "read-b",
                    "output": "result-b",
                },
            ],
        )

    def test_messages_convert_to_responses_items_without_flattening_tools(self):
        call = ToolCall("read_file", {"path": "a.py"}, "read")

        items = _message_items(
            (
                ModelMessage.developer("Use the current workspace."),
                ModelMessage.user("Inspect a.py"),
                ModelMessage.assistant(text="Reading it.", tool_calls=(call,)),
                ModelMessage.tool("read", '{"status":"success"}'),
            )
        )

        self.assertEqual(items[0]["role"], "developer")
        self.assertEqual(items[1]["role"], "user")
        self.assertEqual(items[2]["type"], "message")
        self.assertEqual(items[3]["type"], "function_call")
        self.assertEqual(items[3]["call_id"], "read")
        self.assertEqual(items[4]["type"], "function_call_output")
        self.assertEqual(items[4]["call_id"], "read")

    def test_assistant_text_is_separate_from_hidden_reasoning(self):
        turn = _parse_turn(
            {
                "status": "completed",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 8,
                    "total_tokens": 18,
                    "output_tokens_details": {"reasoning_tokens": 6},
                },
                "output": [
                    {
                        "type": "reasoning",
                        "encrypted_content": "opaque-reasoning",
                    },
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "I will inspect the current file.",
                            }
                        ],
                    },
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "read",
                        "arguments": '{"path":"a.py"}',
                    },
                ],
            },
            self._tools(),
        )

        self.assertEqual(turn.text, "I will inspect the current file.")
        self.assertNotIn("opaque-reasoning", turn.text)
        self.assertEqual(turn.usage["reasoning_tokens"], 6)
        self.assertEqual(_replay_context_tokens(turn), 18)

    def test_submit_final_must_be_alone(self):
        turn = _parse_turn(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "read",
                        "arguments": '{"path":"a.py"}',
                    },
                    {
                        "type": "function_call",
                        "name": "submit_final",
                        "call_id": "final",
                        "arguments": '{"answer":"done"}',
                    },
                ],
            },
            self._tools(),
        )

        self.assertEqual(turn.action.kind, "protocol_error")

    def test_model_action_rejects_duplicate_call_ids(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            ModelAction.tools(
                (
                    ToolCall("read_file", {"path": "a.py"}, "duplicate"),
                    ToolCall("read_file", {"path": "b.py"}, "duplicate"),
                )
            )

    def test_removed_single_call_constructor_is_rejected(self):
        with self.assertRaises(TypeError):
            ModelAction(
                "tool",
                tool_call=ToolCall("read_file", {"path": "a.py"}, "read"),
            )

    def test_more_than_eight_calls_are_rejected(self):
        turn = _parse_turn(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": f"read-{index}",
                        "arguments": '{"path":"a.py"}',
                    }
                    for index in range(9)
                ],
            },
            self._tools(),
        )

        self.assertEqual(turn.action.kind, "protocol_error")
        self.assertIn("at most 8", turn.action.content)

    def test_compaction_schema_does_not_duplicate_the_task_goal(self):
        parameters = SUMMARY_TOOL["parameters"]

        self.assertNotIn("goal", parameters["properties"])
        self.assertNotIn("goal", parameters["required"])

class RepeatedFailureTests(unittest.TestCase):
    def test_different_edit_arguments_are_not_the_same_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n", encoding="utf-8")
            agent, _ = build_agent(root, [
                ModelAction.tool("read_file", {"path": "subject.txt"}),
                *[ModelAction.tool("edit_file", {
                    "path": "subject.txt", "old_text": f"missing-{index}",
                    "new_text": "beta",
                }) for index in range(4)],
                ModelAction.final("No matching text was found."),
            ])
            outcome = agent.ask("Try distinct edits")
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(agent.run.projection.failure.count, 1)
            self.assertEqual((root / "subject.txt").read_text(), "alpha\n")

    def test_argument_key_order_and_call_id_do_not_reset_identical_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            actions = [ModelAction.tool("read_file", args, call_id=f"read-{index}")
                       for index, args in enumerate([
                           {"path": "missing.txt", "start_line": 1},
                           {"start_line": 1, "path": "missing.txt"},
                       ] * 2)]
            agent, model = build_agent(Path(directory), actions)
            outcome = agent.ask("Read a missing file")
            self.assertEqual(outcome.stop_reason, "repeated_failure")
            self.assertEqual(agent.run.projection.failure.count, 4)
            self.assertEqual(len(model.result_batches), 3)
            self.assertIn("retry_instruction", model.result_batches[2][0])
            resets = [
                event
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "provider_session_reset"
            ]
            self.assertEqual(resets, [])

    def test_later_success_in_the_same_batch_cancels_the_failure_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "present.txt").write_text("ok\n", encoding="utf-8")
            missing = {"path": "missing.txt", "start_line": 1}
            agent, model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", missing, call_id="failure-1"),
                    ModelAction.tool("read_file", missing, call_id="failure-2"),
                    ModelAction.tools(
                        (
                            ToolCall("read_file", missing, "failure-3"),
                            ToolCall("read_file", {"path": "present.txt"}, "success"),
                        )
                    ),
                    ModelAction.final("Recovered in the same batch."),
                ],
            )

            outcome = agent.ask("Exercise a mixed third-failure batch")

            self.assertEqual(outcome.status, "completed")
            self.assertIsNone(agent.run.projection.failure)
            self.assertEqual(len(model.result_batches[-1]), 2)
            self.assertTrue(
                all("retry_instruction" not in item for item in model.result_batches[-1])
            )

    def test_different_model_errors_do_not_accumulate(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(Path(directory), [
                *[ModelAction.protocol_error(f"different error {i}") for i in range(4)],
                ModelAction.final("Recovered."),
            ])
            outcome = agent.ask("Exercise distinct errors")
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(len(model.result_batches), 4)
            for batch in model.result_batches:
                self.assertEqual(
                    batch,
                    (
                        (
                            "The previous model response did not match the required "
                            "protocol. Return valid tool calls or one complete final answer."
                        ),
                    ),
                )
                self.assertNotIn("different error", batch[0])
            audit = [
                event.payload["identity"]
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "model_failure"
            ]
            self.assertTrue(any("different error 0" in item for item in audit))

    def test_third_identical_failure_warns_and_fourth_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(
                Path(directory),
                [ModelAction.protocol_error("malformed response")] * 4,
            )

            outcome = agent.ask("Exercise repeated protocol failure")

            self.assertEqual(outcome.status, "stopped")
            self.assertEqual(outcome.stop_reason, "repeated_failure")
            self.assertEqual(agent.run.projection.failure.count, 4)
            self.assertEqual(len(model.requests), 4)
            replayed = replay_events(
                agent.read_run_events(outcome.run_id), expected_run_id=outcome.run_id
            )
            self.assertEqual(replayed.failure.count, 4)
            self.assertEqual(len(model.result_batches), 3)
            self.assertNotIn(
                "same failure has occurred three times",
                model.result_batches[0][0],
            )
            self.assertIn(
                "same failure has occurred three times",
                model.result_batches[2][0],
            )

    def test_provider_failure_stops_without_asking_the_model_to_fix_it(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(
                Path(directory),
                [
                    ModelAction.service_failed("upstream unavailable"),
                    ModelAction.final("must not run"),
                ],
            )

            outcome = agent.ask("Exercise Provider failure")

            self.assertEqual(outcome.status, "stopped")
            self.assertEqual(outcome.stop_reason, "provider_failure")
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(model.result_batches, [])
            self.assertEqual(len(model.actions), 1)

    def test_successful_tool_resets_the_failure_streak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n", encoding="utf-8")
            repeated = ModelAction.tool(
                "edit_file",
                {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
            )
            agent, _model = build_agent(
                root,
                [
                    repeated,
                    repeated,
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "missing", "new_text": "beta"},
                    ),
                    ModelAction.final("Stopped retrying."),
                ],
            )

            outcome = agent.ask("Exercise progress-sensitive retries")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(agent.run.projection.failure.count, 1)


if __name__ == "__main__":
    unittest.main()
