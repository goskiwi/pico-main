import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import SessionStore, TaskContract, ToolCall, ToolOutcome, WriteScope
from pico.history import RunHistory
from pico.run_checkpoint import write_run_checkpoint
from pico.run_log import RunLog, replay_events
from pico.run_store import RunStore


class CheckpointRecoveryTests(unittest.TestCase):
    @staticmethod
    def _new_log(root, *, interval=3):
        store = RunStore(
            Path(root) / "runs",
            checkpoint_event_interval=interval,
            checkpoint_byte_interval=10**9,
        )
        log = RunLog("run_checkpoint", "session_checkpoint", store)
        log.append_user(TaskContract("Inspect", WriteScope("none"), False))
        return store, log

    def test_normal_restore_reads_checkpoint_and_tail_without_full_log(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self._new_log(directory)
            log.append_user_guidance("first")
            log.append("model_requested")
            checkpoint_size = store.checkpoint_path(log.run_id).stat().st_size
            log.append_user_guidance("tail guidance")
            expected_events = store.read_events(log.run_id)
            expected = replay_events(expected_events, expected_run_id=log.run_id)

            restored_store = RunStore(Path(directory) / "runs")
            with mock.patch.object(
                restored_store,
                "_read_events",
                side_effect=AssertionError("full log must not be read"),
            ):
                restored = restored_store.load_run(log.run_id)

            self.assertEqual(restored.projection.summary(), expected.summary())
            self.assertEqual(
                restored.history().latest_user_guidance(), "tail guidance"
            )
            self.assertEqual([event.sequence for event in restored.events], [4])
            self.assertGreater(checkpoint_size, 0)
            appended = restored.append_user_guidance("after restore")
            self.assertEqual(appended.sequence, 5)
            self.assertEqual(
                restored_store.read_events(log.run_id)[-1].payload["content"],
                "after restore",
            )

    def test_damaged_checkpoint_rebuilds_from_current_log(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self._new_log(directory, interval=2)
            log.append_user_guidance("checkpointed")
            log.append("model_requested")
            expected = replay_events(
                store.read_events(log.run_id), expected_run_id=log.run_id
            )
            store.checkpoint_path(log.run_id).write_text("{broken", encoding="utf-8")

            restored_store = RunStore(Path(directory) / "runs")
            restored = restored_store.load_run(log.run_id)

            self.assertEqual(restored.projection.summary(), expected.summary())
            rebuilt = json.loads(
                restored_store.checkpoint_path(log.run_id).read_text(encoding="utf-8")
            )
            self.assertNotIn("schema_version", rebuilt)
            self.assertEqual(rebuilt["last_sequence"], expected.last_sequence)

    def test_checkpoint_failure_never_blocks_the_event_log(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(
                Path(directory) / "runs",
                checkpoint_event_interval=1,
                checkpoint_byte_interval=1,
            )
            log = RunLog("run_checkpoint", "session_checkpoint", store)
            with mock.patch(
                "pico.run_store.write_run_checkpoint",
                side_effect=OSError("checkpoint unavailable"),
            ):
                event = log.append_user(
                    TaskContract("Inspect", WriteScope("none"), False)
                )

            self.assertEqual(event.sequence, 1)
            self.assertEqual(store.read_events(log.run_id)[0], event)

    def test_incomplete_checkpoint_history_rebuilds_from_intact_log(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self._new_log(directory, interval=1)
            call = ToolCall("write_file", {"path": "x", "content": "a"}, "write")
            log.append_tool_intent(call, effect_scope="workspace",
                                   potential_effects=[], operation={})
            log.append_tool_settlement(ToolOutcome(
                "write", "write_file", "success", "completed", "none", "done",
            ))
            expected = replay_events(store.read_events(log.run_id)).summary()
            checkpoint = store.checkpoint_path(log.run_id)
            value = json.loads(checkpoint.read_text())
            value["history"]["events"] = [
                event for event in value["history"]["events"]
                if event["kind"] != "tool_settlement"
            ]
            checkpoint.write_text(json.dumps(value))
            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)
            self.assertEqual(restored.projection.summary(), expected)
            repaired = json.loads(checkpoint.read_text())
            self.assertIn("tool_settlement", [e["kind"] for e in repaired["history"]["events"]])

            # Recovery must not silently accept a damaged authoritative log.
            checkpoint.write_text("{broken")
            store.events_path(log.run_id).write_text("{broken}\n")
            with self.assertRaises(ValueError):
                RunStore(Path(directory) / "runs").load_run(log.run_id)

    def test_pending_intent_is_replayed_only_from_the_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            _store, log = self._new_log(directory, interval=1)
            call = ToolCall(
                "write_file", {"path": "subject.txt", "content": "alpha"}, "write"
            )
            log.append_tool_intent(
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

            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)

            self.assertEqual(restored.pending_tool_call(), call)
            self.assertEqual([event.kind for event in restored.events], ["tool_intent"])

    def test_projection_evidence_metrics_feedback_and_failure_survive_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self._new_log(directory, interval=10**6)
            log.append("model_requested")
            call = ToolCall(
                "write_file", {"path": "subject.txt", "content": "alpha"}, "write"
            )
            log.append_tool_intent(
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
                    "changed",
                    "created",
                    structured={
                        "path_transitions": [
                            {
                                "path": "subject.txt",
                                "before_state": "absent",
                                "after_state": "sha256:after",
                                "before_artifact_id": "",
                            }
                        ]
                    },
                    affected_paths=("subject.txt",),
                    effect_scope="workspace",
                )
            )
            log.append_model_instruction("repair the current state")
            log.append(
                "failure_observed",
                {
                    "category": "completion",
                    "code": "verification_failed",
                    "tool_name": "",
                    "identity": "tests",
                },
            )
            offset = store.events_path(log.run_id).stat().st_size
            write_run_checkpoint(store.checkpoint_path(log.run_id), log, offset)
            expected = log.projection.summary()
            log.append_user_guidance("tail")

            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)

            expected_after_tail = replay_events(
                store.read_events(log.run_id), expected_run_id=log.run_id
            )
            self.assertNotEqual(expected, expected_after_tail.summary())
            self.assertEqual(
                restored.projection.summary(), expected_after_tail.summary()
            )
            self.assertEqual(restored.run_id, log.run_id)


class IncrementalHistoryTests(unittest.TestCase):
    def test_incremental_history_matches_full_reconstruction_after_compaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            log = RunLog("run_history", session.id, store.runs(session.id))
            user = log.append_user(TaskContract("Inspect", WriteScope("none"), False))
            guidance = log.append_user_guidance("latest")
            call = ToolCall("read_file", {"path": "subject.txt"}, "read")
            exchange = log.append_tool_exchange(
                call,
                ToolOutcome(
                    "read",
                    "read_file",
                    "success",
                    "completed",
                    "none",
                    "alpha",
                ),
            )
            log.append_compaction(
                "earlier task summary",
                [user.event_id, guidance.event_id, exchange.event_id],
            )
            full_events = store.runs(session.id).read_events(log.run_id)
            rebuilt = RunHistory(full_events, projected_instruction_id="")

            self.assertEqual(
                log.history().render_projection(), rebuilt.render_projection()
            )
            self.assertEqual(log.history().latest_user_guidance(), "latest")


if __name__ == "__main__":
    unittest.main()
