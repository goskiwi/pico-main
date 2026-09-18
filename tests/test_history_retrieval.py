import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pico import (
    AssistantTurn,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    TaskContract,
    Workspace,
    WriteScope,
)
from pico.artifacts import ArtifactStore, CommandOutputLog
from pico.command_runner import CommandRunner
from pico.compaction_summary import (
    CompactedContext,
    CompactionSummarizer,
    HistoryReference,
    SemanticCompactionError,
)
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.run_store import RunStore
from pico.security import redact_text
from pico.tool_context import ToolContext
from pico.tools import tool_read_history, tool_run_shell
from tests.support import ScriptedModel, assert_file_command, build_agent, request_text


def python_command(source):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


class HistoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = RunStore(self.root / "runs")
        self.artifacts = ArtifactStore(self.store, redact_text)
        self.context = ToolContext(run_id="run_test", tool_call_id="call_test",
                                   execution_context=ExecutionContext.root(max_seconds=10))

    def shell(self, source, *, context=None):
        return tool_run_shell(context or self.context,
                              {"command": python_command(source), "timeout_seconds": 5},
                              command_runner=CommandRunner(self.root, max_output_bytes=1024),
                              workspace_root=self.root, artifact_store=self.artifacts)

    def test_large_shell_middle_is_saved_and_tail_stays_in_context(self):
        result = self.shell("print('HEAD'); print('x'*50000); print('MIDDLE_ERROR'); print('y'*50000); print('TAIL_ERROR')")
        self.assertTrue(result.artifact_id)
        self.assertIn("HEAD", result.content)
        self.assertIn("TAIL_ERROR", result.content)
        self.assertNotIn("MIDDLE_ERROR", result.content)
        self.assertTrue(result.structured["log_complete"])
        full = self.artifacts._source("run_test", result.artifact_id)[1].read_text()
        self.assertIn("MIDDLE_ERROR", full)

    def test_spool_redacts_secrets_split_between_reads(self):
        with mock.patch.dict(os.environ, {"PICO_TEST_TOKEN": "a-cross-read-secret"}):
            log = CommandOutputLog(self.artifacts, "run_test", "split")
            try:
                log.write("stdout", b"x" * 9000 + b"a-cross-")
                log.write("stdout", b"read-secret\n")
                descriptor = log.finish()
            finally:
                log.close()
            full = self.artifacts._source("run_test", descriptor["artifact_id"])[1].read_text()
            self.assertNotIn("a-cross-read-secret", full)
            self.assertIn("<redacted>", full)

    def test_capture_limit_is_explicit_and_discards_partial_final_line(self):
        log = CommandOutputLog(self.artifacts, "run_test", "limit", max_bytes=16)
        try:
            log.write("stdout", b"first\npartial-secret-and-more\n")
            descriptor = log.finish()
        finally:
            log.close()
        full = self.artifacts._source("run_test", descriptor["artifact_id"])[1].read_text()
        self.assertLessEqual(log.size, 16)
        self.assertGreater(log.omitted_bytes, 0)
        self.assertIn("Log incomplete", full)
        self.assertNotIn("partial", full)

    def test_command_continues_after_capture_limit_and_keeps_final_tail(self):
        result = self.shell("print('row\\n'*4500000, flush=True); print('FINAL_TAIL', flush=True)")
        self.assertIsNone(result.failure)
        self.assertIn("FINAL_TAIL", result.content)
        self.assertFalse(result.structured["log_complete"])
        self.assertGreater(result.structured["log_omitted_bytes"], 0)
        path = self.artifacts._source("run_test", result.artifact_id)[1]
        self.assertLessEqual(path.stat().st_size, 16 * 1024 * 1024 + 1024)

    def test_log_publication_failure_reports_it_without_changing_exit_status(self):
        with mock.patch.object(self.artifacts, "write_tool_output", side_effect=OSError("disk full")):
            result = self.shell("print('x'*10000); print('FINAL_TAIL')")
        self.assertIsNone(result.failure)
        self.assertFalse(result.structured["log_complete"])
        self.assertIn("Log unavailable", result.content)
        self.assertIn("FINAL_TAIL", result.content)

    def test_runtime_does_not_retry_failed_log_publication_or_hide_exit_code(self):
        agent, model = build_agent(self.root, [
            ModelAction.tool("run_shell", {"command": python_command("print('x'*100000); print('FINAL_TAIL')")}, call_id="shell"),
            ModelAction.final("Command completed; output log was unavailable."),
        ])
        with mock.patch.object(agent.dependencies.artifacts, "write_tool_output", side_effect=OSError("disk full")) as publish:
            result = agent.ask("Run the command and report its result.")
        self.assertEqual(result.status, "completed")
        publish.assert_called_once()
        output = json.loads(model.results[0])
        self.assertEqual(output["execution_state"], "completed")
        self.assertEqual(output["structured"]["exit_code"], 0)
        self.assertFalse(output["structured"]["log_complete"])
        self.assertIn("FINAL_TAIL", output["content"])

    def test_log_write_failure_does_not_rerun_or_hide_command_effect(self):
        with mock.patch("pico.artifacts.tempfile.TemporaryFile", side_effect=OSError("disk full")):
            result = self.shell("from pathlib import Path; Path('effect.txt').write_text('once'); print('done')")
        self.assertIsNone(result.failure)
        self.assertEqual((self.root / "effect.txt").read_text(), "once")
        self.assertIn("Log unavailable", result.content)
        self.assertFalse(result.structured["log_complete"])

    def test_cancel_and_timeout_preserve_collected_logs(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                context = ExecutionContext.root(max_seconds=0.2 if not cancel else 5)
                timer = threading.Timer(0.2, context.token.request) if cancel else None
                if timer:
                    timer.start()
                try:
                    tool_context = ToolContext(run_id="run_test", tool_call_id=f"stop_{cancel}", execution_context=context)
                    result = self.shell("import time; print('before-stop-'+'x'*10000, flush=True); time.sleep(30)", context=tool_context)
                finally:
                    if timer:
                        timer.cancel()
                        timer.join()
                self.assertEqual(result.failure.code, "command_failed")
                full = self.artifacts._source("run_test", result.artifact_id)[1].read_text()
                self.assertIn("before-stop", full)

    def new_log(self, goal="Inspect"):
        log = RunLog("run_test", "session_test", self.store)
        log.append_user(TaskContract(goal=goal, mode="ask", allowed_tools=("read_history",), write_scope=WriteScope("none")))
        return log

    def test_history_pages_without_replay_and_oversized_event_uses_artifact(self):
        log = self.new_log("original requirement " + "x" * 12000)
        for _ in range(8):
            log.append("user_guidance", {"content": "constraint " + "y" * 1800})
        found, start, artifacts = [], 1, []
        with mock.patch.object(self.store, "read_events", side_effect=AssertionError("must not replay")):
            while True:
                result = tool_read_history(self.context, {"start_sequence": start, "end_sequence": 9},
                                           run_store=self.store, artifact_store=self.artifacts, redact_text=redact_text)
                self.assertLessEqual(len(result.content.encode()), 8192)
                records = [json.loads(line) for line in result.content.splitlines()]
                found.extend(record["sequence"] for record in records)
                artifacts.extend(record["artifact_id"] for record in records if "artifact_id" in record)
                start = result.structured["next_sequence"]
                if start is None:
                    break
        self.assertEqual(found, list(range(1, 10)))
        full = self.artifacts._source("run_test", artifacts[0])[1].read_text()
        self.assertIn("original requirement", full)
        self.assertIn("x" * 12000, full)

    def test_history_ignores_torn_tail_without_repair_and_redacts_before_json(self):
        log = self.new_log("Do not disclose numeric secret 12345")
        path = self.store.events_path(log.run_id)
        original = path.read_bytes() + b'{"sequence":2'
        path.write_bytes(original)
        with mock.patch.dict(os.environ, {"PICO_TEST_TOKEN": "12345"}):
            result = tool_read_history(self.context, {"start_sequence": 1, "end_sequence": 10},
                                       run_store=self.store, artifact_store=self.artifacts, redact_text=redact_text)
        record = json.loads(result.content)
        self.assertEqual(record["sequence"], 1)
        self.assertNotIn("12345", record["payload"]["contract"]["goal"])
        self.assertEqual(path.read_bytes(), original)

    def test_old_compacted_format_is_rejected(self):
        context = CompactedContext((), (), (), (), (), (), (), covered_through_sequence=1).to_dict()
        context.pop("history_refs")
        with self.assertRaisesRegex(ValueError, "invalid CompactedContext"):
            CompactedContext.from_dict(context)

    def test_invalid_reference_is_rejected_and_old_reference_can_be_retained(self):
        previous = CompactedContext((), (), (), (), (), (), (),
                                    history_refs=(HistoryReference(4, 7, "earlier evidence"),),
                                    covered_through_sequence=7)
        groups = ((SimpleNamespace(kind="compaction", payload={"context": previous.to_dict()}, source_event_ids=()),),
                  (SimpleNamespace(kind="user_guidance", payload={"content": "new request"}, source_event_ids=("run_test:event:000010",)),))
        for end, valid in ((7, True), (999, False)):
            with self.subTest(end=end):
                args = {key: value for key, value in previous.to_dict().items()
                        if key not in {"read_files", "modified_files", "covered_through_sequence"}}
                args["history_refs"] = [{"start_sequence": 4, "end_sequence": end, "description": "evidence"}]
                client = mock.Mock()
                client.complete_turn.return_value = AssistantTurn(ModelAction.tool("submit_compaction_summary", args, call_id="summary"))
                summarizer = CompactionSummarizer(lambda client=client: client)
                def summarize(summarizer=summarizer):
                    return summarizer.summarize(groups, task_goal="Inspect", execution_context=self.context.execution_context,
                                                effective_context_limit_tokens=100000, effective_input_limit_tokens=100000,
                                                max_output_tokens=1000, count_tokens=len)
                if valid:
                    self.assertEqual(summarize().history_refs[0].end_sequence, 7)
                else:
                    with self.assertRaises(SemanticCompactionError):
                        summarize()

    def test_compact_restart_retrieve_then_fix_through_ask(self):
        target = self.root / "subject.txt"
        target.write_text("Broken\n")
        workspace = Workspace.build(self.root, repo_root_override=self.root)
        session = SessionStore(self.root / ".pico" / "sessions").create(workspace.root)
        command = python_command("print('x'*50000); print('EXACT_MIDDLE_FAILURE'); print('y'*50000); raise SystemExit(1)")
        model = ScriptedModel([
            ModelAction.tool("read_file", {"path": "subject.txt"}, call_id="read"),
            ModelAction.tool("run_shell", {"command": command}, call_id="test"),
        ])
        agent = Pico(model, workspace, session=session,
                     config=PicoConfig(mode="auto", context_limit_tokens=32000, max_output_tokens=1000,
                                       recent_history_tokens=2000),
                     approval_handler=lambda *_: True)
        agent.dependencies.command_runner = CommandRunner(self.root, max_output_bytes=1024)
        record_results = model.record_action_results
        def crash_after_test(results):
            record_results(results)
            if len(model.result_batches) == 2:
                raise RuntimeError("simulated process crash")
        with (mock.patch.object(model, "record_action_results", side_effect=crash_after_test),
              self.assertRaisesRegex(RuntimeError, "simulated process crash")):
            agent.ask("Change subject.txt to Fixed and check the result.")
        log = agent.run.run_log
        events = agent.read_run_events(log.run_id)
        test_result = next(event for event in events if event.kind == "tool_result" and event.call_id == "test")
        artifact_id = test_result.payload["outcome"]["artifact_id"]
        summary = CompactedContext((), ("Read subject.txt",), ("Fix subject.txt",), ("Command failed",), (), ("Inspect original failure",), (),
                                   history_refs=(HistoryReference(test_result.sequence - 2, test_result.sequence, "failed command"),),
                                   covered_through_sequence=test_result.sequence)
        log.append("compaction", {"context": summary.to_dict()})
        self.assertTrue(agent.dependencies.run_store.checkpoint_if_due(log, force=True))
        resumed_model = ScriptedModel([
            ModelAction.tool("read_history", {"start_sequence": test_result.sequence - 2, "end_sequence": test_result.sequence}, call_id="history"),
            ModelAction.tool("read_artifact", {"artifact_id": artifact_id, "offset": 49950}, call_id="artifact"),
            ModelAction.tool("read_file", {"path": "subject.txt"}, call_id="reread"),
            ModelAction.tool("edit_file", {"path": "subject.txt", "old_text": "Broken", "new_text": "Fixed"}, call_id="edit"),
            ModelAction.tool("run_shell", {"command": assert_file_command("subject.txt", "Fixed\n")}, call_id="verify"),
            ModelAction.final("Fixed and checked."),
        ])
        resumed = Pico(resumed_model, workspace, session=session,
                       config=PicoConfig(mode="auto", context_limit_tokens=32000, max_output_tokens=1000, recent_history_tokens=2000),
                       approval_handler=lambda *_: True)
        result = resumed.ask("Continue.")
        self.assertEqual(result.status, "completed")
        self.assertEqual(resumed.run.run_log.run_id, log.run_id)
        self.assertEqual(target.read_text(), "Fixed\n")
        self.assertIn("failed command", request_text(resumed_model.requests[0]))
        self.assertIn("EXACT_MIDDLE_FAILURE", resumed_model.results[1])


if __name__ == "__main__":
    unittest.main()
