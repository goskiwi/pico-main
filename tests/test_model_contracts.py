import tempfile
import unittest
from pathlib import Path

from pico import ModelAction, ToolCall
from pico.providers.clients import _parse_turn, _result_items
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
            self.assertEqual(agent.run.projection.failure_count, 1)
            self.assertEqual((root / "subject.txt").read_text(), "alpha\n")

    def test_argument_key_order_and_call_id_do_not_reset_identical_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            actions = [ModelAction.tool("read_file", args, call_id=f"read-{index}")
                       for index, args in enumerate([
                           {"path": "missing.txt", "start_line": 1},
                           {"start_line": 1, "path": "missing.txt"},
                       ] * 2)]
            agent, _ = build_agent(Path(directory), actions)
            outcome = agent.ask("Read a missing file")
            self.assertEqual(outcome.stop_reason, "repeated_failure")
            self.assertEqual(agent.run.projection.failure_count, 4)

    def test_different_model_errors_do_not_accumulate(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = build_agent(Path(directory), [
                *[ModelAction.protocol_error(f"different error {i}") for i in range(4)],
                ModelAction.final("Recovered."),
            ])
            outcome = agent.ask("Exercise distinct errors")
            self.assertEqual(outcome.status, "completed")

    def test_third_identical_failure_warns_and_fourth_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(
                Path(directory),
                [ModelAction.protocol_error("malformed response")] * 4,
            )

            outcome = agent.ask("Exercise repeated protocol failure")

            self.assertEqual(outcome.status, "stopped")
            self.assertEqual(outcome.stop_reason, "repeated_failure")
            self.assertEqual(agent.run.projection.failure_count, 4)
            self.assertTrue(agent.run.projection.failure_warned)
            self.assertEqual(len(model.requests), 4)
            replayed = replay_events(
                agent.read_run_events(outcome.run_id), expected_run_id=outcome.run_id
            )
            self.assertEqual(replayed.failure_count, 4)
            self.assertTrue(replayed.failure_warned)
            instructions = [
                event.payload["instruction"]
                for event in agent.run.run_log.events
                if event.kind == "model_instruction"
            ]
            self.assertEqual(
                sum("same failure has occurred three times" in item for item in instructions),
                1,
            )

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
            self.assertEqual(agent.run.projection.failure_count, 1)


if __name__ == "__main__":
    unittest.main()
