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
from tests.support import assert_file_command, build_agent


class RuntimeContractTests(unittest.TestCase):
    def test_multiple_calls_execute_in_order_with_one_model_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("alpha\n", encoding="utf-8")
            (root / "b.txt").write_text("beta\n", encoding="utf-8")
            agent, model = build_agent(
                root,
                [
                    ModelAction.tools(
                        (
                            ToolCall("read_file", {"path": "a.txt"}, "read-a"),
                            ToolCall("read_file", {"path": "b.txt"}, "read-b"),
                        )
                    ),
                    ModelAction.final("Read both files."),
                ],
            )

            outcome = agent.ask("Read a.txt and b.txt")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(len(model.requests), 2)
            self.assertEqual(len(model.result_batches), 1)
            self.assertEqual(len(model.result_batches[0]), 2)
            exchanges = [
                event
                for event in agent.run.run_log.events
                if event.kind == "tool_exchange"
            ]
            self.assertEqual(
                [event.call_id for event in exchanges],
                ["read-a", "read-b"],
            )

    def test_multiple_mutations_keep_one_ordered_durable_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            agent, model = build_agent(
                root,
                [
                    ModelAction.tool(
                        "read_file", {"path": "subject.txt"}, call_id="read"
                    ),
                    ModelAction.tools(
                        (
                            ToolCall(
                                "edit_file",
                                {
                                    "path": "subject.txt",
                                    "old_text": "alpha\n",
                                    "new_text": "beta\n",
                                },
                                "edit",
                            ),
                            ToolCall(
                                "write_file",
                                {"path": "new.txt", "content": "created\n"},
                                "write",
                            ),
                        )
                    ),
                    ModelAction.final("Updated both files."),
                ],
            )

            outcome = agent.ask("Update subject.txt and create new.txt")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")
            self.assertEqual((root / "new.txt").read_text(), "created\n")
            self.assertEqual(len(model.requests), 3)
            transactions = [
                (event.kind, event.call_id)
                for event in agent.run.run_log.events
                if event.kind in {"tool_intent", "tool_settlement"}
            ]
            self.assertEqual(
                transactions,
                [
                    ("tool_intent", "edit"),
                    ("tool_settlement", "edit"),
                    ("tool_intent", "write"),
                    ("tool_settlement", "write"),
                ],
            )

    def test_terminal_record_rejects_removed_final_diff_field(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = build_agent(Path(directory), [ModelAction.final("Done")])
            outcome = agent.ask("Inspect")
            event = agent.read_run_events(outcome.run_id)[-1].to_dict()
            event["payload"]["final_diff"] = None
            with self.assertRaises(ValueError):
                RunEvent.from_dict(event)

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

    def test_read_edit_test_and_complete_through_pico_ask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            check = assert_file_command("subject.txt", "beta\n")
            approval = mock.Mock(return_value=True)
            agent, model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}, call_id="read"),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                        call_id="edit",
                    ),
                    ModelAction.tool(
                        "run_shell", {"command": check}, call_id="test"
                    ),
                    ModelAction.final("Updated subject.txt."),
                ],
                approval_handler=approval,
            )

            outcome = agent.ask("Replace alpha with beta and test it")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")
            self.assertFalse(hasattr(outcome, "changed_paths"))
            self.assertNotIn("final_diff", outcome.to_dict())
            self.assertFalse(list(root.rglob("preimage_*")))
            self.assertFalse(list(root.rglob("diff_*")))
            self.assertEqual(len(model.requests), 4)
            self.assertEqual(agent.run.metrics.tool_counts["run_shell"], 1)
            self.assertEqual(approval.call_count, 1)
            self.assertEqual(approval.call_args.args[0], "run_shell")
            shell_intent = next(
                event
                for event in agent.run.run_log.events
                if event.kind == "tool_intent" and event.call_id == "test"
            )
            self.assertEqual(
                shell_intent.payload["operation"],
                {
                    "command": check,
                    "cwd": ".",
                    "environment_policy": "minimal",
                    "shell": "/bin/sh",
                    "timeout_seconds": 120,
                },
            )
            shell_result = next(
                event for event in agent.run.run_log.events
                if event.kind == "tool_settlement" and event.call_id == "test"
            ).payload["outcome"]
            self.assertEqual(shell_result["status"], "success")
            self.assertEqual(shell_result["side_effect_state"], "unknown")
            self.assertEqual(shell_result["effect_scope"], "workspace")
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
                    ("tool_intent", "test"),
                    ("tool_settlement", "test"),
                ],
            )
            replayed = replay_events(
                agent.read_run_events(outcome.run_id), expected_run_id=outcome.run_id
            )
            self.assertEqual(replayed.summary(), agent.run.projection.summary())

    def test_edit_uses_current_content_without_a_prior_read(self):
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

            outcome = agent.ask("Edit the file")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(), "beta\n")
            tool_events = [
                event
                for event in agent.run.run_log.events
                if event.kind in {"tool_exchange", "tool_intent", "tool_settlement"}
            ]
            self.assertEqual(
                [event.kind for event in tool_events],
                ["tool_intent", "tool_settlement"],
            )
            result = ToolOutcome.from_dict(tool_events[-1].payload["outcome"])
            self.assertEqual(result.status, "success")

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

    def test_failed_model_run_test_is_repaired_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")
            check = assert_file_command("subject.txt", "beta\n")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "broken\n"},
                    ),
                    ModelAction.tool("run_shell", {"command": check}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "broken\n", "new_text": "beta\n"},
                    ),
                    ModelAction.tool("run_shell", {"command": check}),
                    ModelAction.final("Repaired and verified."),
                ],
            )

            outcome = agent.ask("Make subject.txt contain beta")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")
            self.assertEqual(agent.run.metrics.tool_counts["run_shell"], 2)
            self.assertEqual(agent.run.metrics.outcome_counts["error"], 1)

    def test_submit_final_does_not_run_a_command_implicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approval = mock.Mock(return_value=True)
            agent, _model = build_agent(
                root,
                [ModelAction.final("Done without running a check.")],
                approval_handler=approval,
            )

            outcome = agent.ask("Inspect the workspace")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(agent.run.metrics.executed_tool_count, 0)
            approval.assert_not_called()

    def test_auto_mode_exposes_shell_but_never_bypasses_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approval = mock.Mock(return_value=False)
            agent, model = build_agent(
                root,
                [
                    ModelAction.tool(
                        "run_shell",
                        {"command": "printf denied > denied.txt"},
                        call_id="shell",
                    ),
                    ModelAction.final("The command was not approved."),
                ],
                approval_handler=approval,
            )

            outcome = agent.ask("Run a command")

            self.assertEqual(outcome.status, "completed")
            self.assertFalse((root / "denied.txt").exists())
            self.assertIn(
                "run_shell",
                {tool["name"] for tool in model.requests[0]["action_tools"]},
            )
            approval.assert_called_once()
            shell_event = next(
                event
                for event in agent.run.run_log.events
                if event.call_id == "shell"
            )
            rejected = ToolOutcome.from_dict(shell_event.payload["outcome"])
            self.assertEqual(rejected.failure.code, "approval_denied")

    def test_shell_timeout_must_fit_the_declared_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approval = mock.Mock(return_value=True)
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool(
                        "run_shell",
                        {"command": "printf should-not-run", "timeout_seconds": 601},
                        call_id="shell",
                    ),
                    ModelAction.final("The invalid timeout was rejected."),
                ],
                approval_handler=approval,
            )

            outcome = agent.ask("Run a command with an invalid timeout")

            self.assertEqual(outcome.status, "completed")
            approval.assert_not_called()
            shell_event = next(
                event
                for event in agent.run.run_log.events
                if event.call_id == "shell"
            )
            rejected = ToolOutcome.from_dict(shell_event.payload["outcome"])
            self.assertEqual(rejected.execution_state, "not_started")
            self.assertEqual(rejected.failure.code, "invalid_arguments")

    def test_removed_acceptance_protocol_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported Run Log kind"):
            RunEvent(
                event_id="run_old:event:000001",
                sequence=1,
                run_id="run_old",
                session_id="session_old",
                kind="verification_result",
                timestamp="2026-09-12T00:00:00+00:00",
                payload={},
            )
        with self.assertRaisesRegex(ValueError, "task contract"):
            TaskContract.from_dict(
                {
                    "goal": "Inspect",
                    "write_scope": {"mode": "none", "paths": []},
                    "verification_required": False,
                }
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
            self.assertFalse(list(root.rglob("preimage_*")))
            transitions = outcome.structured["path_transitions"]
            self.assertEqual(set(transitions[0]), {"path", "before_state", "after_state"})

    def test_interrupted_shell_is_observed_then_the_agent_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, model = build_agent(
                root,
                [
                    ModelAction.tool(
                        "run_shell",
                        {"command": "printf x >> generated.txt"},
                        call_id="interrupted-shell",
                    ),
                    ModelAction.tool(
                        "run_shell",
                        {"command": "test -f generated.txt"},
                        call_id="inspect-shell-result",
                    ),
                    ModelAction.final("Inspected the interrupted command and continued."),
                ],
            )
            real_append = RunLog.append_tool_settlement

            def lose_first_settlement(log, outcome, **kwargs):
                if outcome.tool_call_id == "interrupted-shell":
                    raise OSError("simulated settlement loss")
                return real_append(log, outcome, **kwargs)

            with mock.patch.object(
                RunLog,
                "append_tool_settlement",
                new=lose_first_settlement,
            ), self.assertRaisesRegex(OSError, "settlement loss"):
                agent.ask("Create an output and inspect it")

            outcome = agent.ask("Continue from the interrupted command")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual((root / "generated.txt").read_text(), "x")
            self.assertIn(
                "settled without replaying it",
                model.requests[1]["input_text"],
            )
            recovered = next(
                event
                for event in agent.run.run_log.events
                if event.call_id == "interrupted-shell"
                and event.kind == "tool_settlement"
            )
            self.assertTrue(recovered.payload["recovered_from_interruption"])
            self.assertEqual(
                recovered.payload["outcome"]["side_effect_state"], "unknown"
            )

    def test_compaction_cannot_split_call_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            run_store = store.runs(session.id)
            log = RunLog("run_compaction", session.id, run_store)
            user = log.append_user(
                TaskContract("Inspect", WriteScope("none"))
            )
            call = ToolCall("write_file", {"path": "subject.txt", "content": "alpha"}, "write")
            call_event = log.append_tool_intent(
                call,
                effect_scope="workspace",
                potential_effects=[
                    {
                        "path": "subject.txt",
                        "before_state": "absent",
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


class FileMutationLifecycleTests(unittest.TestCase):
    def test_edit_preserves_unrelated_change_made_after_read(self):
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
                    ModelAction.final("Preserved the external edit."),
                ],
                before_action=change_after_initial_read,
            )

            outcome = agent.ask("Change alpha to beta and preserve other work")
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(), "beta\nexternal\n")
            settlement = next(
                event for event in agent.run.run_log.events
                if event.kind == "tool_settlement"
            )
            transition = settlement.payload["outcome"]["structured"]["path_transitions"][0]
            self.assertTrue(transition["before_state"].startswith("sha256:"))
            self.assertFalse(list(root.rglob("preimage_*")))
            failures = [
                event.payload["outcome"]["failure"]["code"]
                for event in agent.run.run_log.events
                if event.kind in {"tool_exchange", "tool_settlement"}
                and event.payload["outcome"].get("failure")
            ]
            self.assertEqual(failures, [])

    def test_edit_rejects_when_external_change_removes_target_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "subject.txt"
            target.write_text("alpha\n", encoding="utf-8")

            def change_after_initial_read(index):
                if index == 1:
                    target.write_text("external\n", encoding="utf-8")

            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.tool(
                        "edit_file",
                        {"path": "subject.txt", "old_text": "alpha\n", "new_text": "beta\n"},
                    ),
                    ModelAction.final("The target changed before editing."),
                ],
                before_action=change_after_initial_read,
            )

            outcome = agent.ask("Change alpha to beta")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(), "external\n")
            failure = next(
                event.payload["outcome"]["failure"]["code"]
                for event in agent.run.run_log.events
                if event.kind in {"tool_exchange", "tool_settlement"}
                and event.payload["outcome"].get("failure")
            )
            self.assertEqual(failure, "text_not_found")

    def test_successful_edit_and_completion_do_not_rescan_file_state(self):
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
                    ),
                    ModelAction.final("Edited."),
                ],
            )

            with mock.patch.object(
                agent.workspace, "path_state", wraps=agent.workspace.path_state
            ) as path_state:
                outcome = agent.ask("Change alpha to beta")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(target.read_text(), "beta\n")
            path_state.assert_not_called()

    def test_failed_edit_does_not_create_a_change(self):
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
            self.assertNotIn("final_diff", outcome.to_dict())
            failure = next(
                event.payload["outcome"]["failure"]["code"]
                for event in agent.run.run_log.events
                if event.kind in {"tool_exchange", "tool_settlement"}
                and event.payload["outcome"].get("failure")
            )
            self.assertEqual(failure, "text_not_found")

    def test_noop_and_revert_preserve_transaction_history_without_backups(self):
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
            self.assertNotIn("final_diff", outcome.to_dict())
            settlements = [
                event.payload["outcome"]
                for event in agent.run.run_log.events
                if event.kind == "tool_settlement"
            ]
            self.assertEqual(
                [item["side_effect_state"] for item in settlements],
                ["none", "changed", "changed"],
            )
            self.assertFalse(list(root.rglob("preimage_*")))


if __name__ == "__main__":
    unittest.main()
