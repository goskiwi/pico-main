import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
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
from pico.cli import _memory_command
from pico.memory import MemoryStore
from pico.memory_worker import MemoryWorker
from pico.run_log import RunLog
from pico.security import redact_text
from pico.tools import tool_read_memory
from tests.support import ScriptedModel, request_text


def changes(content="Use pytest, not unittest.", filename="feedback_testing.md", source="source"):
    return {"updates": [{"filename": filename, "type": "feedback", "name": "Testing preference",
                         "description": "Preferred testing approach", "content": content, "source_event_ids": [source]}],
            "deletes": []}


class Extractor(ScriptedModel):
    def __init__(self, content="Use pytest, not unittest."):
        super().__init__([])
        self.content = content
        self.closed = False

    def complete_turn(self, messages, max_output_tokens, **kwargs):
        prompt = json.loads(messages[0].text)
        source = (prompt["material"]["user_messages"][0]["event_id"]
                  if "user_messages" in prompt["material"] else prompt["source"])
        exists = any(item["filename"] == "feedback_testing.md" for item in prompt["directory"]["entries"])
        action = (ModelAction.tool("read_memory", {"filename": "feedback_testing.md"}, call_id="read")
                  if exists and not self.requests else ModelAction.tool("submit_memory_changes", changes(self.content, source=source), call_id="save"))
        self.actions.append(action)
        return super().complete_turn(messages, max_output_tokens, **kwargs)

    def close(self):
        self.closed = True


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MemoryStore(self.root, redact_text)
        self.sessions = SessionStore(self.root / ".pico" / "sessions")

    def seed(self, **kwargs):
        with self.store.writer():
            state = self.store.state()
            self.store.prepare(state, "request_seed", changes(**kwargs), source_ids={"source"}, read_files={"feedback_testing.md"})
            self.store.recover(state)

    def worker(self, factory):
        return MemoryWorker(self.store, self.sessions, self.root, factory, count_tokens=len, input_limit=100000, output_limit=2000)

    def run_log(self, goal="Please prefer pytest.", *, completed=True):
        session = self.sessions.create(self.root)
        run_store = self.sessions.runs(session.id)
        log = RunLog("run_test", session.id, run_store)
        log.append_user(TaskContract(goal=goal, mode="ask", allowed_tools=("read_memory",), write_scope=WriteScope("none")))
        log.append("run_started", {"workspace_root": str(self.root)})
        if completed:
            log.append("model_requested")
            log.append_assistant_turn(AssistantTurn(ModelAction.final("Acknowledged.")))
        return session, log

    def test_atomic_topic_format_provenance_and_redaction(self):
        with mock.patch.dict(os.environ, {"PICO_TEST_TOKEN": "test-private-secret"}):
            self.seed(content="Prefer pytest. Never store test-private-secret.")
            topic = self.store.read("feedback_testing.md")
        self.assertIn("<redacted>", topic["content"])
        self.assertEqual(topic["source"], "request_seed")
        self.assertEqual(topic["source_event_ids"], ["source"])
        self.assertEqual(self.store.catalog()["entries"][0]["type"], "feedback")

    def test_partial_apply_recovers_same_plan_without_model_or_duplicates(self):
        plan = changes()
        plan["updates"].append(changes("Prefer small diffs.", "feedback_diffs.md")["updates"][0])
        state = self.store.state()
        self.store.prepare(state, "session/run", plan, source_ids={"source"}, read_files=set())
        from pico.memory import atomic_replace_bytes
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated write interruption")
            return atomic_replace_bytes(*args, **kwargs)
        with mock.patch("pico.memory.atomic_replace_bytes", side_effect=fail_second), self.assertRaises(OSError):
            self.store.recover(state)
        self.assertIsNotNone(self.store.state()["pending"])
        self.assertNotIn("session/run", self.store.state()["processed_runs"])
        restarted = MemoryStore(self.root, redact_text)
        restarted.recover(restarted.state())
        self.assertEqual(len(restarted.catalog()["entries"]), 2)
        self.assertEqual(restarted.state()["processed_runs"], ["session/run"])
        self.assertIsNone(restarted.state()["pending"])

    def test_ack_failure_keeps_pending_plan_for_recovery(self):
        state = self.store.state()
        self.store.prepare(state, "session/run", changes(), source_ids={"source"}, read_files=set())
        with mock.patch.object(self.store, "save_state", side_effect=OSError("ack failed")), self.assertRaises(OSError):
            self.store.recover(state)
        self.assertIsNotNone(state["pending"])
        self.store.recover(self.store.state())
        self.assertEqual(self.store.state()["processed_runs"], ["session/run"])

    def test_extract_update_and_deduplicate_by_existing_topic(self):
        session, log = self.run_log()
        first = Extractor()
        self.assertEqual(self.worker(lambda: first).process_pending(), 1)
        self.assertTrue(first.closed)
        self.assertIn(f"{session.id}/{log.run_id}", self.store.state()["processed_runs"])
        factory = mock.Mock(side_effect=AssertionError("already processed"))
        self.assertEqual(self.worker(factory).process_pending(), 0)
        self.store.remember("Correction: use the existing integration tests.")
        second = Extractor("Use the existing integration tests.")
        self.assertEqual(self.worker(lambda: second).process_pending(), 1)
        self.assertEqual(len(self.store.catalog()["entries"]), 1)
        self.assertEqual(self.store.read("feedback_testing.md")["content"], second.content)
        self.assertEqual(second.requests[0]["action_tools"][1]["name"], "list_memories")
        self.assertEqual(len(second.requests), 2)
        self.assertEqual(self.store.state()["requests"], [])

    def test_failure_backoff_does_not_mark_source_processed(self):
        session, log = self.run_log()
        client = ScriptedModel([ModelAction.service_failed("temporary outage")])
        worker = self.worker(lambda: client)
        worker.process_pending()
        key = f"{session.id}/{log.run_id}"
        self.assertNotIn(key, self.store.state()["processed_runs"])
        self.assertGreater(self.store.state()["failures"][key]["retry_after"], 0)
        self.assertEqual(worker.process_pending(), 0)
        with mock.patch("pico.memory_worker.time.time", return_value=self.store.state()["failures"][key]["retry_after"] + 1):
            recovered = self.worker(lambda: Extractor())
            self.assertEqual(recovered.process_pending(), 1)
        self.assertIn(key, self.store.state()["processed_runs"])

    def test_active_and_other_project_runs_are_not_extracted(self):
        self.run_log(completed=False)
        other = self.root / "other"
        other.mkdir()
        self.sessions.create(other)
        factory = mock.Mock(side_effect=AssertionError("must not extract"))
        self.assertEqual(self.worker(factory).process_pending(), 0)
        factory.assert_not_called()

    def test_unsupported_session_does_not_block_current_completed_run(self):
        old = self.sessions.root / "old_session"
        old.mkdir()
        (old / "session.json").write_text('{"schema_version":"old"}')
        session, log = self.run_log()
        worker = self.worker(lambda: Extractor())
        self.assertEqual(worker.process_pending(), 1)
        self.assertIn(f"{session.id}/{log.run_id}", self.store.state()["processed_runs"])
        self.assertTrue(worker.source_errors)

    def test_path_symlink_and_invalid_source_rejected_before_mutation(self):
        for filename in ("../outside.md", "/etc/passwd", "nested/file.md"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.store.prepare(self.store.state(), "request_test", changes(filename=filename), source_ids={"source"}, read_files=set())
        with self.assertRaises(ValueError):
            self.store.prepare(self.store.state(), "request_test", changes(source="invented"), source_ids={"source"}, read_files=set())
        outside = self.root / "outside.md"
        outside.write_text("untouched")
        (self.store.root / "topics" / "feedback_testing.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.store.read("feedback_testing.md")
        self.assertEqual(outside.read_text(), "untouched")

    def test_update_requires_read_and_body_paging_keeps_exact_content(self):
        self.seed(content="A" * 2001)
        with self.assertRaises(ValueError):
            self.store.prepare(self.store.state(), "request_test", changes(), source_ids={"source"}, read_files=set())
        first = tool_read_memory(None, {"filename": "feedback_testing.md", "offset": 0, "limit": 1000}, memory_store=self.store)
        last = tool_read_memory(None, {"filename": "feedback_testing.md", "offset": 2000, "limit": 1000}, memory_store=self.store)
        self.assertEqual(first.structured["next_offset"], 1000)
        self.assertEqual(last.content, "A")
        self.assertIsNone(last.structured["next_offset"])

    def test_older_retried_source_cannot_override_or_resurrect_deleted_topic(self):
        state = self.store.state()
        self.store.prepare(state, "request_new", changes(content="New preference"), source_ids={"source"}, read_files=set(), source_timestamp=200)
        self.store.recover(state)
        self.store.prepare(state, "old_session/run", changes(content="Old preference"), source_ids={"source"},
                           read_files={"feedback_testing.md"}, source_timestamp=100)
        self.store.recover(state)
        self.assertEqual(self.store.read("feedback_testing.md")["content"], "New preference")
        self.store.prepare(state, "old_goal/recent_run", changes(content="Old goal incorrectly presented as new"),
                           source_ids={"source"}, read_files={"feedback_testing.md"}, source_timestamp=250,
                           source_times={"source": 100})
        self.store.recover(state)
        self.assertEqual(self.store.read("feedback_testing.md")["content"], "New preference")
        self.assertIn("old_session/run", self.store.state()["processed_runs"])
        with mock.patch("pico.memory.time.time", return_value=300):
            self.store.forget("feedback_testing.md")
        state = self.store.state()
        self.store.prepare(state, "other_old/run", changes(), source_ids={"source"}, read_files=set(), source_timestamp=250)
        self.store.recover(state)
        self.assertEqual(self.store.catalog()["entries"], [])
        self.store.prepare(state, "request_later", changes(content="Explicitly remember again"), source_ids={"source"}, read_files=set(), source_timestamp=400)
        self.store.recover(state)
        self.assertEqual(self.store.read("feedback_testing.md")["content"], "Explicitly remember again")

    def test_partial_read_cannot_authorize_whole_topic_replacement(self):
        self.seed(content="A" * 2001)
        partial = Extractor("Should not replace unread suffix")
        self.store.remember("Update this long memory")
        worker = self.worker(lambda: partial)
        worker.process_pending()
        self.assertIn("read existing memory", worker.last_error)
        self.assertEqual(self.store.read("feedback_testing.md")["content"], "A" * 2001)

    def test_malformed_memory_does_not_block_new_agent_task(self):
        workspace = Workspace.build(self.root, repo_root_override=self.root)
        model = ScriptedModel([ModelAction.final("Current task answered.")])
        model.new_isolated_client = lambda: Extractor()
        config = PicoConfig(memory_enabled=True, context_limit_tokens=32000, max_output_tokens=2000, recent_history_tokens=2000)
        agent = Pico(model, workspace, self.sessions.create(self.root), config=config)
        self.addCleanup(agent.close)
        (self.store.root / "topics" / "broken.md").write_text("not a supported memory topic")
        self.assertEqual(agent.ask("Answer this self-contained task.").status, "completed")
        self.assertNotIn("broken.md", request_text(model.requests[0]))

    def test_foreground_is_nonblocking_and_new_run_reads_memory_after_restart(self):
        workspace = Workspace.build(self.root, repo_root_override=self.root)
        session = self.sessions.create(self.root)
        main = ScriptedModel([ModelAction.final("Acknowledged.")])
        entered, release = threading.Event(), threading.Event()
        extraction = Extractor()
        original = extraction.complete_turn
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test extraction wait expired")
            return original(*args, **kwargs)
        extraction.complete_turn = blocked
        main.new_isolated_client = lambda: extraction
        config = PicoConfig(memory_enabled=True, mode="ask", context_limit_tokens=32000, max_output_tokens=2000, recent_history_tokens=2000)
        agent = Pico(main, workspace, session, config=config)
        self.addCleanup(agent.close)
        result = agent.ask("Use pytest in future work.")
        self.assertEqual(result.status, "completed")
        self.assertTrue(entered.wait(2))
        self.assertFalse(release.is_set())  # ask returned before the extractor finished.
        release.set()
        self.assertTrue(agent.dependencies.memory_worker.idle.wait(5))
        agent.close()
        next_model = ScriptedModel([
            ModelAction.tool("read_memory", {"filename": "feedback_testing.md"}, call_id="memory"),
            ModelAction.final("I will use pytest."),
        ])
        next_model.new_isolated_client = lambda: Extractor()
        new_session = self.sessions.create(self.root)
        restarted = Pico(next_model, workspace, new_session, config=config)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.ask("Which testing approach should we use?").status, "completed")
        self.assertIn("feedback_testing.md", request_text(next_model.requests[0]))
        self.assertIn("Use pytest", next_model.results[0])
        restarted.dependencies.memory_worker.idle.wait(5)
        restarted.forget_memory("feedback_testing.md")
        self.assertEqual(self.store.catalog()["entries"], [])
        self.assertEqual(restarted.dependencies.memory_worker.process_pending(), 0)
        self.assertNotIn("feedback_testing.md", restarted.memory_index())

    def test_worker_is_single_writer_and_forget_recovers_pending_first(self):
        self.seed()
        state = self.store.state()
        self.store.prepare(state, "request_update", changes(content="Updated"), source_ids={"source"}, read_files={"feedback_testing.md"})
        with self.store.writer():
            self.assertEqual(self.worker(lambda: Extractor()).process_pending(), 0)
        self.store.forget("feedback_testing.md")
        self.assertEqual(self.store.catalog()["entries"], [])
        self.assertIsNone(self.store.state()["pending"])

    def test_memory_tools_and_management_disabled_by_default(self):
        workspace = Workspace.build(self.root, repo_root_override=self.root)
        model = ScriptedModel([])
        agent = Pico(model, workspace, self.sessions.create(self.root),
                     config=PicoConfig(context_limit_tokens=32000, max_output_tokens=2000, recent_history_tokens=2000))
        self.assertNotIn("read_memory", agent.tools.registry)
        with self.assertRaises(ValueError):
            _memory_command(agent, "/memory remember preference")

    def test_cli_management_remember_status_paginate_and_forget(self):
        model = ScriptedModel([])
        model.new_isolated_client = lambda: Extractor()
        agent = Pico(model, Workspace.build(self.root, repo_root_override=self.root), self.sessions.create(self.root),
                     config=PicoConfig(memory_enabled=True, context_limit_tokens=32000, max_output_tokens=2000, recent_history_tokens=2000))
        self.addCleanup(agent.close)
        self.assertIn("queued", _memory_command(agent, "/memory remember Use pytest"))
        self.assertTrue(agent.dependencies.memory_worker.idle.wait(5))
        self.assertEqual(json.loads(_memory_command(agent, "/memory status"))["queued_requests"], 0)
        self.assertEqual(json.loads(_memory_command(agent, "/memory list 0"))["entries"][0]["filename"], "feedback_testing.md")
        self.assertEqual(json.loads(_memory_command(agent, "/memory list 1"))["entries"], [])
        self.assertIn("deleted", _memory_command(agent, "/memory forget feedback_testing.md"))

    def test_memory_input_budget_failure_keeps_run_unprocessed(self):
        session, log = self.run_log()
        client = Extractor()
        worker = self.worker(lambda: client)
        worker.input_limit = 1
        worker.process_pending()
        self.assertIn("exceeds budget", worker.last_error)
        self.assertEqual(client.requests, [])
        self.assertNotIn(f"{session.id}/{log.run_id}", self.store.state()["processed_runs"])


if __name__ == "__main__":
    unittest.main()
