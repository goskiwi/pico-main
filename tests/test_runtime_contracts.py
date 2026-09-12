import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import (
    ModelAction,
    SessionStore,
    TaskContract,
    ToolCall,
    ToolOutcome,
    WriteScope,
)
from pico.providers import ProviderContextOverflow
from pico.run_log import RunEvent, RunLog, replay_events
from tests.support import build_agent, verification_command


class RuntimeContractTests(unittest.TestCase):
    def test_context_overflow_rebuilds_then_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(Path(directory), [])
            with mock.patch.object(model, "complete_action", side_effect=[
                ProviderContextOverflow("window exceeded"), ModelAction.final("Recovered"),
            ]) as request, mock.patch.object(
                model, "estimate_action_input_tokens", side_effect=[6000, 1000],
            ):
                outcome = agent.ask("Inspect")
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(request.call_count, 2)
            events = agent.read_run_events(outcome.run_id)
            resets = [e for e in events if e.kind == "provider_session_reset"]
            self.assertEqual([e.payload["reason"] for e in resets], ["context_overflow_retry"])
            self.assertEqual(replay_events(events).summary(), agent.run.projection.summary())

    def test_context_overflow_without_reduction_does_not_request_again(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(Path(directory), [])
            with mock.patch.object(model, "complete_action", side_effect=ProviderContextOverflow("window exceeded")) as request, mock.patch.object(
                model, "estimate_action_input_tokens", return_value=6000,
            ), self.assertRaisesRegex(ProviderContextOverflow, "did not reduce"):
                agent.ask("Inspect")
            self.assertEqual(request.call_count, 1)
            self.assertTrue(agent.run.resumable)

    def test_cancellation_after_verification_preserves_evidence_before_stopping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n")
            agent, _ = build_agent(root, [ModelAction.final("Ready")],
                                   verification=verification_command("subject.txt", "alpha\n"),
                                   verification_required=True)
            verify = agent.run_verification
            def verify_then_cancel(sequence, policy):
                result = verify(sequence, policy)
                agent.cancel_current_run()
                return result
            with mock.patch.object(agent, "run_verification", side_effect=verify_then_cancel):
                outcome = agent.ask("Check the file")
            self.assertEqual(outcome.stop_reason, "user_cancelled")
            self.assertEqual(agent.run.evidence.latest_verification["status"], "passed")
            events = agent.read_run_events(outcome.run_id)
            self.assertLess(next(e.sequence for e in events if e.kind == "verification_result"),
                            next(e.sequence for e in events if e.kind == "run_stopped"))
            self.assertEqual(agent.session.active_run_id, "")
            self.assertIsNone(agent.run.execution_context)

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
            tool_events = [
                (event.kind, event.call_id)
                for event in agent.run.run_log.events
                if event.kind in {"tool_exchange", "tool_intent", "tool_settlement"}
            ]
            self.assertEqual(
                tool_events,
                [
                    ("tool_exchange", "read"),
                    ("tool_intent", "edit"),
                    ("tool_settlement", "edit"),
                    ("tool_intent", "verify"),
                    ("tool_settlement", "verify"),
                ],
            )
            replayed = replay_events(
                agent.read_run_events(outcome.run_id), expected_run_id=outcome.run_id
            )
            self.assertEqual(replayed.summary(), agent.run.projection.summary())

    def test_pre_execution_rejection_is_one_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                        call_id="edit_without_read",
                    ),
                    ModelAction.final("No edit was made."),
                ],
            )

            outcome = agent.ask("Attempt an edit")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(), "alpha\n")
            tool_events = [
                event
                for event in agent.run.run_log.events
                if event.kind in {"tool_exchange", "tool_intent", "tool_settlement"}
            ]
            self.assertEqual([event.kind for event in tool_events], ["tool_exchange"])
            rejected = ToolOutcome.from_dict(tool_events[0].payload["outcome"])
            self.assertEqual(rejected.execution_state, "not_started")
            self.assertEqual(rejected.failure.code, "read_required")

    def test_removed_tool_event_format_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported Run Log kind"):
            RunEvent(
                event_id="run_old:event:000001",
                sequence=1,
                run_id="run_old",
                session_id="session_old",
                kind="tool_result",
                timestamp="2026-09-12T00:00:00+00:00",
                payload={"outcome": {}},
            )

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

    def test_failed_intent_persistence_prevents_the_write(self):
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
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                    ),
                ],
            )

            with mock.patch.object(
                RunLog,
                "append_tool_intent",
                side_effect=OSError("simulated intent failure"),
            ), self.assertRaisesRegex(OSError, "intent failure"):
                agent.ask("Replace alpha with beta")

            self.assertEqual(target.read_text(), "alpha\n")
            self.assertIsNone(agent.run.run_log.pending_tool_call())

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
            real_append = RunLog.append_tool_settlement

            def fail_edit_settlement(log, outcome, **kwargs):
                if outcome.tool_name == "edit_file":
                    raise OSError("simulated settlement failure")
                return real_append(log, outcome, **kwargs)

            with mock.patch.object(
                RunLog, "append_tool_settlement", new=fail_edit_settlement
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
            call = ToolCall("write_file", {"path": "subject.txt", "content": "alpha"}, "write")
            call_event = log.append_tool_intent(
                call,
                effect_scope="workspace",
                potential_effects=[
                    {
                        "path": "subject.txt",
                        "before_state": "absent",
                        "before_artifact_id": "",
                    }
                ],
                operation={},
            )
            log.append_tool_settlement(
                ToolOutcome(
                    "write",
                    "write_file",
                    "success",
                    "completed",
                    "none",
                    "no change",
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
                if event.kind in {"tool_exchange", "tool_settlement"}
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
                if event.kind in {"tool_exchange", "tool_settlement"}
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
