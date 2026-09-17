import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import (
    AssistantTurn,
    ModelAction,
    TaskContract,
    ToolCall,
    ToolOutcome,
    WriteScope,
)
from pico.compaction_summary import CompactedContext
from pico.failure_policy import guidance_for_failure
from pico.history import HistoryBudgetExceeded, RunHistory
from pico.run_checkpoint import read_run_checkpoint
from pico.run_log import RunLog, replay_events
from pico.run_store import RunStore


def compacted_context(summary="summary", *, covered=1, read=(), modified=()):
    return CompactedContext(
        constraints=(summary,),
        progress_done=("done",),
        progress_in_progress=("continue",),
        progress_blocked=(),
        key_decisions=("decision",),
        next_steps=("next",),
        critical_context=("context",),
        read_files=tuple(read),
        modified_files=tuple(modified),
        covered_through_sequence=covered,
    )


class CheckpointRecoveryTests(unittest.TestCase):
    @staticmethod
    def new_log(root, *, interval=3):
        store = RunStore(
            Path(root) / "runs",
            checkpoint_event_interval=interval,
            checkpoint_byte_interval=10**9,
        )
        log = RunLog("run_checkpoint", "session_checkpoint", store)
        log.append_user(
            TaskContract(
                goal="Inspect",
                mode="ask",
                allowed_tools=("read_file",),
                write_scope=WriteScope("none"),
            )
        )
        store.checkpoint_if_due(log, force=True)
        return store, log

    @staticmethod
    def append_turn(log, calls, outcomes, *, started=()):
        calls = tuple(calls)
        action = (
            ModelAction.tool(calls[0].name, calls[0].args, call_id=calls[0].call_id)
            if len(calls) == 1
            else ModelAction.tools(calls)
        )
        log.append("model_requested")
        log.append_assistant_turn(AssistantTurn(action))
        for call, outcome in zip(calls, outcomes, strict=True):
            if call.call_id in started:
                log.append_tool_started(
                    call.call_id,
                    effect_scope="workspace",
                    potential_effects=[],
                    operation={},
                )
            log.append_tool_result(outcome)

    def test_checkpoint_is_one_ready_boundary_with_task_and_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            value = json.loads(store.checkpoint_path(log.run_id).read_text())

            self.assertEqual(
                set(value),
                {
                    "run_id",
                    "session_id",
                    "last_sequence",
                    "event_log_offset",
                    "run_state",
                    "context_state",
                },
            )
            self.assertEqual(
                set(value["run_state"]),
                {"contract", "status", "metrics", "failure"},
            )
            self.assertEqual(
                set(value["context_state"]),
                {"compacted", "recent_events"},
            )
            self.assertIsNone(value["context_state"]["compacted"])
            self.assertEqual(value["context_state"]["recent_events"], [])

    def test_checkpoint_plus_tail_restores_inflight_model_request(self):
        with tempfile.TemporaryDirectory() as directory:
            _store, log = self.new_log(directory)
            log.append("model_requested")

            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)

            self.assertEqual(restored.projection.phase, "requesting_model")
            self.assertEqual(restored.projection.last_sequence, 2)

    def test_checkpoint_plus_tail_restores_partial_multi_tool_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            _store, log = self.new_log(directory)
            first = ToolCall("read_file", {"path": "a.py"}, "read-a")
            second = ToolCall("read_file", {"path": "b.py"}, "read-b")
            log.append("model_requested")
            log.append_assistant_turn(AssistantTurn(ModelAction.tools((first, second))))
            log.append_tool_result(
                ToolOutcome("read-a", "read_file", "success", "completed", "none", "a")
            )

            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)

            active = restored.projection.active_tool_turn
            self.assertEqual(restored.projection.phase, "executing_tools")
            self.assertEqual(active.completed_call_ids, ("read-a",))
            self.assertEqual(active.next_call, second)

    def test_checkpoint_cursor_advances_only_after_a_complete_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory, interval=1)
            path = store.checkpoint_path(log.run_id)
            initial = json.loads(path.read_text())["last_sequence"]
            first = ToolCall("read_file", {"path": "a.py"}, "read-a")
            second = ToolCall("read_file", {"path": "b.py"}, "read-b")

            log.append("model_requested")
            self.assertFalse(store.checkpoint_if_due(log))
            log.append_assistant_turn(AssistantTurn(ModelAction.tools((first, second))))
            self.assertFalse(store.checkpoint_if_due(log))
            log.append_tool_result(
                ToolOutcome("read-a", "read_file", "success", "completed", "none", "a")
            )
            self.assertFalse(store.checkpoint_if_due(log))
            self.assertEqual(
                json.loads(path.read_text())["last_sequence"],
                initial,
            )
            log.append_tool_result(
                ToolOutcome("read-b", "read_file", "success", "completed", "none", "b")
            )

            self.assertTrue(store.checkpoint_if_due(log))
            self.assertEqual(
                json.loads(path.read_text())["last_sequence"],
                log.projection.last_sequence,
            )

    def test_checkpoint_plus_tail_restores_started_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            _store, log = self.new_log(directory)
            call = ToolCall("write_file", {"path": "x", "content": "a"}, "write")
            log.append("model_requested")
            log.append_assistant_turn(
                AssistantTurn(ModelAction.tool(call.name, call.args, call_id=call.call_id))
            )
            log.append_tool_started(
                call.call_id,
                effect_scope="workspace",
                potential_effects=[{"path": "x", "before_state": "absent"}],
                operation={},
            )

            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)

            self.assertEqual(restored.projection.pending_tool.call, call)
            self.assertEqual(restored.projection.phase, "executing_tools")

    def test_model_failure_is_checkpointed_at_the_next_ready_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            log.append("model_requested")
            log.append_model_failure(
                "invalid",
                "untrusted partial output",
                "untrusted partial output",
                {},
            )
            store.checkpoint_if_due(log, force=True)

            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)
            correction = guidance_for_failure(restored.projection.failure)

            self.assertIn("incomplete", correction)
            self.assertNotIn("untrusted partial output", correction)

    def test_damaged_checkpoint_rebuilds_only_at_a_ready_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            log.append_user_guidance("checkpointed")
            store.checkpoint_path(log.run_id).write_text("{broken")

            restored_store = RunStore(Path(directory) / "runs")
            restored = restored_store.load_run(log.run_id)

            self.assertEqual(restored.projection.phase, "ready_for_model")
            rebuilt = json.loads(restored_store.checkpoint_path(log.run_id).read_text())
            self.assertEqual(
                rebuilt["last_sequence"],
                restored.projection.last_sequence,
            )

    def test_damaged_checkpoint_does_not_hide_an_inflight_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            log.append("model_requested")
            store.checkpoint_path(log.run_id).write_text("{broken")

            restored = RunStore(Path(directory) / "runs").load_run(log.run_id)

            self.assertEqual(restored.projection.phase, "requesting_model")
            self.assertEqual(store.checkpoint_path(log.run_id).read_text(), "{broken")

    def test_checkpoint_failure_never_blocks_the_event_log(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            log.append_user_guidance("continue")
            with mock.patch(
                "pico.run_store.write_run_checkpoint",
                side_effect=OSError("checkpoint unavailable"),
            ):
                written = store.checkpoint_if_due(log, force=True)

            self.assertFalse(written)
            self.assertEqual(store.read_events(log.run_id)[-1].kind, "user_guidance")

    def test_checkpoint_reuses_compacted_context_without_a_second_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            context = compacted_context(
                "constraint",
                covered=log.append_user_guidance("old context").sequence,
                read=("src/config.py",),
                modified=("src/login.py",),
            )
            log.append_compaction(context)
            store.checkpoint_if_due(log, force=True)

            checkpoint = json.loads(store.checkpoint_path(log.run_id).read_text())

            self.assertEqual(
                checkpoint["context_state"]["compacted"], context.to_dict()
            )
            self.assertNotIn("goal", checkpoint["context_state"]["compacted"])
            self.assertEqual(checkpoint["context_state"]["recent_events"], [])

    def test_removed_checkpoint_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            path = store.checkpoint_path(log.run_id)
            value = json.loads(path.read_text())
            value["history"] = value.pop("context_state")
            path.write_text(json.dumps(value))

            with self.assertRaisesRegex(ValueError, "checkpoint fields"):
                read_run_checkpoint(path, expected_run_id=log.run_id)


class IncrementalHistoryTests(unittest.TestCase):
    @staticmethod
    def new_log(directory, run_id="run_history"):
        store = RunStore(Path(directory) / "runs")
        log = RunLog(run_id, "session_history", store)
        log.append_user(
            TaskContract(
                goal="Inspect",
                mode="auto",
                allowed_tools=("read_file", "edit_file"),
                write_scope=WriteScope("workspace"),
            )
        )
        return store, log

    @staticmethod
    def append_result_turn(log, call, outcome, *, started=False):
        CheckpointRecoveryTests.append_turn(
            log,
            (call,),
            (outcome,),
            started=(call.call_id,) if started else (),
        )

    @staticmethod
    def plan(log, summary="summary"):
        semantic = compacted_context(summary, covered=1)
        return log.history().plan_compaction(
            retain_tokens=1,
            token_counter=len,
            context_budget=lambda _events: (50_000, len),
            context_size=lambda events, text: len(text)
            + sum(len(event.content) for event in events),
            summary_builder=lambda _facts, **_kwargs: semantic,
        )

    def test_compaction_tracks_and_merges_file_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            _store, log = self.new_log(directory)
            for call_id, path in (
                ("read-config", "src/config.py"),
                ("read-login", "src/login.py"),
            ):
                call = ToolCall("read_file", {"path": path}, call_id)
                self.append_result_turn(
                    log,
                    call,
                    ToolOutcome(
                        call_id,
                        "read_file",
                        "success",
                        "completed",
                        "none",
                        "source " * 200,
                        structured={"path": path},
                    ),
                )
            edit = ToolCall("edit_file", {"path": "src/login.py"}, "edit-login")
            self.append_result_turn(
                log,
                edit,
                ToolOutcome(
                    edit.call_id,
                    edit.name,
                    "success",
                    "completed",
                    "changed",
                    "diff " * 200,
                    affected_paths=("src/login.py",),
                ),
                started=True,
            )

            first = self.plan(log, "first")
            log.append_compaction(first)

            self.assertEqual(first.read_files, ("src/config.py",))
            self.assertEqual(first.modified_files, ("src/login.py",))

            later = ToolCall("read_file", {"path": "src/routes.py"}, "read-routes")
            self.append_result_turn(
                log,
                later,
                ToolOutcome(
                    later.call_id,
                    later.name,
                    "success",
                    "completed",
                    "none",
                    "routes " * 200,
                    structured={"path": "src/routes.py"},
                ),
            )
            second = self.plan(log, "second")
            log.append_compaction(second)

            self.assertEqual(
                second.read_files,
                ("src/config.py", "src/routes.py"),
            )
            self.assertEqual(second.modified_files, ("src/login.py",))

    def test_compacted_context_is_required_history(self):
        with tempfile.TemporaryDirectory() as directory:
            _store, log = self.new_log(directory)
            old = log.append_user_guidance("old context")
            context = compacted_context("x" * 500, covered=old.sequence)
            log.append_compaction(context)

            with self.assertRaisesRegex(
                HistoryBudgetExceeded,
                "committed compaction summary",
            ):
                log.history().render_compacted_projection(
                    retain_tokens=100,
                    token_counter=len,
                )

    def test_checkpoint_and_full_replay_render_the_same_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            old = log.append_user_guidance("old context")
            context = compacted_context(
                "earlier summary",
                covered=old.sequence,
                read=("src/config.py",),
            )
            log.append_compaction(context)
            log.append_user_guidance("preserve the API")
            store.checkpoint_if_due(log, force=True)

            restored = RunStore(store.root).load_run(log.run_id)
            rebuilt = RunHistory(store.read_events(log.run_id))

            self.assertEqual(restored.context_state.compacted, context)
            self.assertNotIn(
                "compaction",
                [event.kind for event in restored.context_state.recent_events],
            )
            self.assertEqual(
                restored.history().render_projection(),
                rebuilt.render_projection(),
            )
            self.assertEqual(
                restored.history().user_texts(),
                rebuilt.user_texts(),
            )
            self.assertEqual(
                restored.history().model_message_units(),
                rebuilt.model_message_units(),
            )

    def test_live_projection_matches_full_event_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self.new_log(directory)
            call = ToolCall("read_file", {"path": "a.py"}, "read")
            self.append_result_turn(
                log,
                call,
                ToolOutcome(
                    "read", "read_file", "success", "completed", "none", "a"
                ),
            )

            replayed = replay_events(store.read_events(log.run_id))

            self.assertEqual(log.projection.summary(), replayed.summary())


if __name__ == "__main__":
    unittest.main()
