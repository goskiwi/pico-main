import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from pico.command_runner import CommandRunner, shell_argv
from pico.execution import ExecutionContext
from pico.tool_context import ToolContext
from pico.tools import tool_run_shell


class CommandRunnerTests(unittest.TestCase):
    def test_timeout_terminates_the_process_group_and_keeps_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(directory, max_output_bytes=1024)
            started = time.monotonic()
            result = runner.run(
                shell_argv("printf started; sleep 30"),
                cwd=directory,
                timeout=0.2,
            )

            self.assertLess(time.monotonic() - started, 3)
            self.assertIsNone(result.returncode)
            self.assertEqual(result.stop_reason, "deadline_exceeded")
            self.assertIn("started", result.stdout)


class RunShellSettlementTests(unittest.TestCase):
    def test_global_deadline_still_allows_bounded_repository_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(("git", "add", "tracked.txt"), cwd=root, check=True)
            subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Pico Test",
                    "-c",
                    "user.email=pico@example.invalid",
                    "commit",
                    "-qm",
                    "initial",
                ),
                cwd=root,
                check=True,
            )
            context = ToolContext(
                run_id="run_test",
                tool_call_id="call_test",
                execution_context=ExecutionContext.root(max_seconds=2),
            )

            result = tool_run_shell(
                context,
                {"command": "printf 'after\\n' > tracked.txt; sleep 30"},
                command_runner=CommandRunner(root),
                workspace_root=root,
            )

            self.assertEqual(result.failure.code, "command_modified_repository")
            self.assertIn("tracked.txt", result.structured["repository_changes"])
            self.assertEqual(result.structured["stop_reason"], "deadline_exceeded")


if __name__ == "__main__":
    unittest.main()
