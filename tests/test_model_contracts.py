import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from pico import ModelAction
from pico.providers.clients import _parse_turn
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
        self.assertIsNone(turn.action.tool_call)
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


class CompletionRequirementTests(unittest.TestCase):
    @staticmethod
    def _passing_command():
        return f"{shlex.quote(sys.executable)} -c {shlex.quote('pass')}"

    def test_required_verification_runs_without_workspace_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(
                Path(directory),
                [ModelAction.final("Verified without changes.")],
                verification=self._passing_command(),
                verification_required=True,
            )

            outcome = agent.ask("Run the required acceptance check")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(
                agent.run.evidence.latest_verification["status"], "passed"
            )

    def test_workspace_change_does_not_imply_verification_when_not_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {
                            "path": "subject.txt",
                            "old_text": "alpha\n",
                            "new_text": "beta\n",
                        },
                    ),
                    ModelAction.final("Changed without an acceptance requirement."),
                ],
                verification=self._passing_command(),
                verification_required=False,
            )

            outcome = agent.ask("Change the file")

            self.assertEqual(outcome.status, "completed")
            self.assertIsNone(agent.run.evidence.latest_verification)


class RepeatedFailureTests(unittest.TestCase):
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
