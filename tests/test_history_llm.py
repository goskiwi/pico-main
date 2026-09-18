"""Opt-in real-model context handoff test; runs only in a disposable workspace."""

import os
import runpy
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pico import Pico, PicoConfig, SessionStore, Workspace
from pico.cli import _build_model_client
from pico.command_runner import CommandRunner
from pico.env import load_project_env
from pico.execution import ExecutionContext


@unittest.skipUnless(os.environ.get("PICO_HISTORY_LLM_TEST") == "1", "opt-in real LLM test")
class RealHistoryHandoffTests(unittest.TestCase):
    def test_real_summary_restart_retrieve_and_fix(self):
        project = Path(__file__).resolve().parents[1]
        load_project_env(project, boundary=project)
        if not os.environ.get("PICO_OPENAI_API_KEY"):
            self.fail("PICO_OPENAI_API_KEY is required")
        with tempfile.TemporaryDirectory(prefix="pico-history-llm-") as directory:
            root = Path(directory)
            (root / "source.py").write_text("VALUE = 1\n")
            (root / "check.py").write_text(
                "from source import VALUE\n"
                "if VALUE != 2:\n"
                "    print('BEGIN TEST OUTPUT', flush=True)\n"
                "    print('noise-before-' * 40000, flush=True)\n"
                "    print('EXACT_MIDDLE_FAILURE: source.VALUE must be 2, currently 1', flush=True)\n"
                "    print('noise-after-' * 40000, flush=True)\n"
                "    print('FAILED: consult the full output for the assertion', flush=True)\n"
                "    raise SystemExit(1)\n"
                "print('1 passed')\n"
            )
            original_check = (root / "check.py").read_bytes()
            command = f"{shlex.quote(sys.executable)} check.py"
            goal = (
                f"Fix the failure reported by `{command}`. First run that exact command with run_shell. "
                "Do not modify check.py. After a context handoff, use read_history to recover the failed "
                "command and read_artifact to find the exact assertion in the middle of its full output "
                "(approximately byte offset 519950). Read current source.py, fix it, run the same check "
                "again and submit an evidence-backed final answer."
            )
            workspace = Workspace.build(root, repo_root_override=root)
            session = SessionStore(root / ".pico" / "sessions").create(workspace.root)
            config = PicoConfig(mode="auto", context_limit_tokens=64000, max_output_tokens=2000,
                                recent_history_tokens=2000, allowed_write_paths=("source.py",),
                                max_model_requests_per_attempt=16, attempt_timeout_seconds=180)
            model = _build_model_client(SimpleNamespace(model=None))
            self.addCleanup(model.close)
            agent = Pico(model, workspace, session=session, config=config, approval_handler=lambda *_: True)
            agent.dependencies.command_runner = CommandRunner(root, max_output_bytes=1024)
            original_record = model.record_action_results

            def interrupt_after_failed_shell(results):
                original_record(results)
                for event in agent.run.run_log.context_state.recent_events:
                    if event.kind == "tool_result":
                        outcome = event.payload["outcome"]
                        if outcome["tool_name"] == "run_shell" and outcome["failure"]:
                            raise RuntimeError("test context handoff")

            with (mock.patch.object(model, "record_action_results", side_effect=interrupt_after_failed_shell),
                  self.assertRaisesRegex(RuntimeError, "test context handoff")):
                agent.ask(goal)
            log = agent.run.run_log
            run_id = log.run_id
            history = log.history()
            groups = tuple(history._projection_units(history.recent_events(), include_user_guidance=True))
            summary = agent.prompt.semantic_summarizer.summarize(
                groups, task_goal=goal, execution_context=ExecutionContext.root(max_seconds=120),
                effective_context_limit_tokens=64000, effective_input_limit_tokens=62000,
                max_output_tokens=2000, count_tokens=agent.prompt.count_tokens,
            )
            self.assertTrue(summary.history_refs, "real summary must retain evidence references")
            read_files, modified_files = history._compacted_file_lists(groups, None)
            summary = summary.with_runtime_facts(read_files=read_files, modified_files=modified_files,
                                                covered_through_sequence=history.recent_events()[-1].sequence)
            log.append("compaction", {"context": summary.to_dict()})
            self.assertTrue(agent.dependencies.run_store.checkpoint_if_due(log, force=True))
            model.close()

            resumed_model = _build_model_client(SimpleNamespace(model=None))
            self.addCleanup(resumed_model.close)
            resumed = Pico(resumed_model, workspace, session=session, config=config, approval_handler=lambda *_: True)
            result = resumed.ask("Continue the original task. Recover the referenced history and full error output before fixing.")
            self.assertEqual(result.status, "completed")
            self.assertEqual(resumed.run.run_log.run_id, run_id)
            events = resumed.read_run_events(run_id)
            outcomes = [event.payload["outcome"] for event in events if event.kind == "tool_result"]
            self.assertTrue(any(item["tool_name"] == "read_history" and item["status"] == "success" for item in outcomes))
            self.assertTrue(any(item["tool_name"] == "read_artifact" and "EXACT_MIDDLE_FAILURE" in item["content"] for item in outcomes))
            self.assertTrue(any(item["tool_name"] == "run_shell" and "1 passed" in item["content"] for item in outcomes))
            self.assertEqual((root / "check.py").read_bytes(), original_check)
            self.assertEqual(runpy.run_path(str(root / "source.py"))["VALUE"], 2)
            check = subprocess.run([sys.executable, "check.py"], cwd=root, capture_output=True, timeout=10, check=False)
            self.assertEqual(check.returncode, 0)


if __name__ == "__main__":
    unittest.main()
