import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pico.command_runner import CommandRunner, shell_argv
from pico.execution import ExecutionContext
from pico.tool_context import ToolContext
from pico.tools import tool_run_shell


class CommandRunnerTests(unittest.TestCase):
    def test_detached_descendant_does_not_block_pipe_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "descendant.pid"
            source = (
                "import subprocess,sys,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)'],start_new_session=True); "
                "pathlib.Path('descendant.pid').write_text(str(p.pid)); "
                "print('leader done',flush=True)"
            )
            try:
                start = time.monotonic()
                result = CommandRunner(directory).run(
                    (sys.executable, "-c", source), cwd=directory, timeout=5,
                )
                self.assertLess(time.monotonic() - start, 3)
                self.assertEqual(result.stop_reason, "pipe_held_open")
                self.assertIn("leader done", result.stdout)
            finally:
                if pid_path.exists():
                    try:
                        os.kill(int(pid_path.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_input_and_both_output_pipes_do_not_deadlock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ("import sys; sys.stdout.write('o'*200000); sys.stdout.flush(); "
                      "sys.stderr.write('e'*200000); sys.stderr.flush(); "
                      "assert len(sys.stdin.buffer.read()) == 200000")
            result = CommandRunner(directory, max_output_bytes=1024).run_bytes(
                (sys.executable, "-c", source), cwd=directory, timeout=5,
                input_bytes=b"x" * 200000,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stop_reason, "")
            self.assertEqual(len(result.stdout), 512)
            self.assertEqual(len(result.stderr), 512)
            self.assertEqual(result.stdout_discarded_bytes, 200000 - 512)
            self.assertEqual(result.stderr_discarded_bytes, 200000 - 512)

    def test_cancel_with_blocked_stdin_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ExecutionContext.root(max_seconds=5)
            timer = threading.Timer(0.2, context.request_stop)
            timer.start()
            start = time.monotonic()
            try:
                result = CommandRunner(directory).run_bytes(
                    (sys.executable, "-c", "import time; time.sleep(15)"),
                    cwd=directory, timeout=5, execution_context=context,
                    input_bytes=b"x" * 200000,
                )
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual(result.stop_reason, "user_cancelled")

    def test_large_output_is_drained_into_bounded_buffers(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(directory, max_output_bytes=1024)
            result = runner.run(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('o'*200000); sys.stderr.write('e'*200000)",
                ),
                cwd=directory,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.output_limited)
            self.assertGreater(result.stdout_discarded_bytes, 0)
            self.assertGreater(result.stderr_discarded_bytes, 0)
            self.assertIn("stdout truncated; discarded", result.stdout)
            self.assertIn("stderr truncated; discarded", result.stderr)
            self.assertLess(len(result.stdout.encode()) + len(result.stderr.encode()), 1400)

    def test_exact_internal_output_reports_overflow_instead_of_using_a_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(directory, max_output_bytes=1024)
            result = runner.run_bytes(
                (sys.executable, "-c", "print('x'*10000)"),
                cwd=directory,
                timeout=5,
                require_complete_output=True,
            )

            self.assertTrue(result.output_limited)
            self.assertTrue(result.infrastructure_error)

    def test_descendant_holding_pipes_is_killed_after_a_bounded_grace_period(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(directory, max_output_bytes=1024)
            source = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
                "print('leader done')"
            )
            started = time.monotonic()
            result = runner.run(
                (sys.executable, "-c", source),
                cwd=directory,
                timeout=5,
            )

            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stop_reason, "pipe_held_open")
            self.assertIn("leader done", result.stdout)

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
