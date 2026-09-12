import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import (
    ModelAction,
    Pico,
    SessionStore,
    TaskContract,
    ToolCall,
    ToolOutcome,
    WriteScope,
)
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunLog, replay_events
from tests.support import ScriptedModel, build_agent, verification_command


class RuntimeContractTests(unittest.TestCase):
    def test_read_edit_verify_and_complete_through_pico_ask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            verify = verification_command("subject.txt", "beta\n")
            agent, model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}, call_id="read"),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                        call_id="edit",
                    ),
                    ModelAction.tool("verify", {}, call_id="verify"),
                    ModelAction.final("Updated subject.txt."),
                ],
                verification=verify,
            )

            outcome = agent.ask("Replace alpha with beta and verify it")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")
            self.assertEqual(outcome.changed_paths, ("subject.txt",))
            self.assertTrue(outcome.final_diff.artifact_id)
            self.assertEqual(len(model.requests), 4)
            self.assertEqual(agent.run.evidence.latest_verification["status"], "passed")
            replayed = replay_events(
                agent.read_run_events(outcome.run_id), expected_run_id=outcome.run_id
            )
            self.assertEqual(replayed.summary(), agent.run.projection.summary())

    def test_failed_verification_is_repaired_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            verify = verification_command("subject.txt", "beta\n")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "broken\n"},
                    ),
                    ModelAction.tool("verify", {}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "broken\n", "new_text": "beta\n"},
                    ),
                    ModelAction.final("Repaired and verified."),
                ],
                verification=verify,
            )

            outcome = agent.ask("Make subject.txt contain beta")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")
            self.assertGreaterEqual(
                agent.run.metrics.verification_counts.get("failed", 0), 1
            )
            self.assertGreaterEqual(
                agent.run.metrics.verification_counts.get("passed", 0), 1
            )

    def test_call_saved_before_start_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(root, [])
            RunLifecycle(agent).initialize("Inspect subject.txt")
            call = ToolCall("edit_file", {"path": "subject.txt", "old_text": "alpha", "new_text": "beta"}, "pending")
            agent.run.run_log.append_tool_call(call)
            agent.run.execution_context = None

            restored = Pico(
                ScriptedModel([]),
                agent.workspace,
                session=agent.session.store.load(agent.session.id),
                config=agent.config,
            )
            outcome, _entry = restored.tools.reconcile_interrupted()

            self.assertEqual(outcome.execution_state, "not_started")
            self.assertEqual(outcome.side_effect_state, "none")
            self.assertEqual(outcome.failure.code, "operation_not_started")
            self.assertEqual((root / "subject.txt").read_text(), "alpha\n")

    def test_crash_after_edit_before_result_is_settled_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}, call_id="read"),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                        call_id="edit",
                    ),
                ],
            )
            real_append = RunLog.append_tool_result

            def fail_edit_settlement(log, outcome, **kwargs):
                if outcome.tool_name == "edit_file":
                    raise OSError("simulated settlement failure")
                return real_append(log, outcome, **kwargs)

            with mock.patch.object(
                RunLog, "append_tool_result", new=fail_edit_settlement
            ), self.assertRaisesRegex(OSError, "settlement failure"):
                agent.ask("Replace alpha with beta")

            self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")
            recovered = agent.tools.reconcile_interrupted()
            self.assertIsNotNone(recovered)
            outcome, _entry = recovered
            self.assertEqual(outcome.status, "partial_success")
            self.assertEqual(outcome.execution_state, "failed")
            self.assertEqual(outcome.side_effect_state, "partial")
            self.assertEqual(outcome.affected_paths, ("subject.txt",))
            self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")

    def test_compaction_cannot_split_call_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            run_store = store.runs(session.id)
            log = RunLog("run_compaction", session.id, run_store)
            user = log.append_user(
                TaskContract("Inspect", WriteScope("none"), False)
            )
            call = ToolCall("read_file", {"path": "subject.txt"}, "read")
            call_event = log.append_tool_call(call)
            log.append_tool_started(call, effect_scope="none", potential_effects=[], operation={})
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

            with self.assertRaisesRegex(ValueError, "split"):
                log.append_compaction("summary", [user.event_id, call_event.event_id])


class PreimageLifecycleTests(unittest.TestCase):
    def test_external_change_is_reread_before_edit_and_becomes_the_preimage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")

            def change_after_initial_read(index):
                if index == 1:
                    target.write_text("alpha\nexternal\n", encoding="utf-8")

            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                    ),
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {
                            "path": "subject.txt",
                            "old_text": "alpha\nexternal\n",
                            "new_text": "beta\nexternal\n",
                        },
                    ),
                    ModelAction.final("Preserved the external edit."),
                ],
                before_action=change_after_initial_read,
            )

            outcome = agent.ask("Change alpha to beta and preserve other work")

            self.assertEqual(target.read_text(), "beta\nexternal\n")
            change = agent.run.evidence.change_set.files["subject.txt"]
            self.assertEqual(
                agent.dependencies.artifacts.read_internal_text(
                    outcome.run_id, change.first_before_artifact_id
                ),
                "alpha\nexternal\n",
            )
            failures = [
                event.payload["outcome"]["failure"]["code"]
                for event in agent.run.run_log.events
                if event.kind == "tool_result"
                and event.payload["outcome"].get("failure")
            ]
            self.assertIn("revision_conflict", failures)

    def test_failed_edit_does_not_create_a_net_change_or_final_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "missing\n", "new_text": "beta\n"},
                    ),
                    ModelAction.final("No change was possible."),
                ],
            )

            outcome = agent.ask("Try the requested replacement")

            self.assertEqual(target.read_text(), "alpha\n")
            self.assertEqual(outcome.changed_paths, ())
            self.assertEqual(outcome.final_diff.artifact_id, "")
            failure = next(
                event.payload["outcome"]["failure"]["code"]
                for event in agent.run.run_log.events
                if event.kind == "tool_result"
                and event.payload["outcome"].get("failure")
            )
            self.assertEqual(failure, "text_not_found")

    def test_noop_and_revert_preserve_first_preimage_and_empty_final_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "alpha\n"},
                    ),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                    ),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "beta\n", "new_text": "alpha\n"},
                    ),
                    ModelAction.final("No net change."),
                ],
            )

            outcome = agent.ask("Exercise edits and restore the original")

            self.assertEqual(target.read_text(), "alpha\n")
            self.assertEqual(outcome.changed_paths, ())
            self.assertEqual(outcome.final_diff.artifact_id, "")
            change = agent.run.evidence.change_set.files["subject.txt"]
            self.assertTrue(change.first_before_artifact_id.startswith("preimage_"))
            self.assertEqual(
                agent.dependencies.artifacts.read_internal_text(
                    outcome.run_id, change.first_before_artifact_id
                ),
                "alpha\n",
            )


if __name__ == "__main__":
    unittest.main()
