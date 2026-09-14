import tempfile
import time
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
    ToolCall,
    ToolOutcome,
    Workspace,
    WriteScope,
)
from pico.compaction_summary import CompactedContext, SemanticCompactionError
from pico.providers import ProviderContextOverflow
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunEvent, RunLog, replay_events
from tests.support import ScriptedModel, assert_file_command, build_agent


class RuntimeContractTests(unittest.TestCase):
    @staticmethod
    def _agent_with_limits(root, actions, **limits):
        workspace = Workspace.build(root, repo_root_override=root)
        session = SessionStore(root / ".pico" / "sessions").create(workspace.root)
        model = ScriptedModel(actions)
        agent = Pico(
            model,
            workspace,
            session=session,
            config=PicoConfig(
                mode="auto",
                context_limit_tokens=32_000,
                recent_history_tokens=2_000,
                max_output_tokens=1_000,
                **limits,
            ),
            approval_handler=lambda _name, _args, _plan: True,
        )
        return agent, model

    def test_model_request_limit_is_scoped_to_one_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, model = self._agent_with_limits(
                root,
                [
                    ModelAction.protocol_error("first"),
                    ModelAction.protocol_error("second"),
                    ModelAction.final("must not run"),
                ],
                max_model_requests_per_attempt=2,
            )

            outcome = agent.ask("Exercise the request limit")

            self.assertEqual(outcome.stop_reason, "model_request_limit")
            self.assertEqual(len(model.requests), 2)

    def test_run_status_combines_task_and_bounded_context(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(Path(directory), [])
            RunLifecycle(agent).initialize("Fix login")
            old = agent.run.run_log.append_user_guidance("Keep the public API")
            agent.run.run_log.append_compaction(
                CompactedContext(
                    constraints=("Keep the public API",),
                    progress_done=("Found the failing branch",),
                    progress_in_progress=("Editing login.py",),
                    progress_blocked=(),
                    key_decisions=("Preserve the parser",),
                    next_steps=("Run focused tests",),
                    critical_context=(),
                    read_files=("src/login.py",),
                    modified_files=(),
                    covered_through_sequence=old.sequence,
                )
            )
            agent.run.run_log.append_user_guidance("Do not change the database")

            status = agent.run_status()

            self.assertEqual(
                status["run"]["task"]["contract"]["goal"],
                "Fix login",
            )
            compacted = status["context"]["compacted"]
            self.assertEqual(
                compacted["progress"]["in_progress"],
                ["Editing login.py"],
            )
            self.assertEqual(compacted["next_steps"], ["Run focused tests"])
            self.assertEqual(compacted["read_files"], ["src/login.py"])
            self.assertEqual(
                status["context"]["recent_events"][0]["payload"]["content"],
                "Do not change the database",
            )

    def test_attempt_timeout_settles_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, model = self._agent_with_limits(
                root,
                [ModelAction.final("must not complete")],
                attempt_timeout_seconds=1,
            )
            model.before_action = lambda _index: time.sleep(1.05)

            outcome = agent.ask("Exercise the attempt timeout")

            self.assertEqual(outcome.status, "stopped")
            self.assertEqual(outcome.stop_reason, "attempt_timeout")

    def test_user_cancellation_settles_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, model = self._agent_with_limits(
                root,
                [ModelAction.final("must not complete")],
            )
            model.before_action = lambda _index: agent.cancel_current_run()

            outcome = agent.ask("Exercise cancellation")

            self.assertEqual(outcome.status, "stopped")
            self.assertEqual(outcome.stop_reason, "user_cancelled")

    def test_run_pointer_is_durable_before_the_first_event(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _model = build_agent(
                Path(directory),
                [ModelAction.final("unused")],
            )

            def fail_before_event(log, _contract):
                self.assertEqual(agent.session.active_run_id, log.run_id)
                raise OSError("simulated first Event failure")

            with mock.patch.object(
                RunLog,
                "append_user",
                new=fail_before_event,
            ), self.assertRaisesRegex(OSError, "first Event failure"):
                agent.ask("start")

            self.assertEqual(agent.session.active_run_id, "")

    def test_empty_pointed_run_is_cleared_and_orphan_runs_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = SessionStore(root / "sessions")
            session = sessions.create(root)
            run_store = sessions.runs(session.id)
            orphan = RunLog("run_orphan", session.id, run_store)
            orphan.append_user(TaskContract("orphan", WriteScope("none")))

            runtime = Pico(
                ScriptedModel([]),
                Workspace.build(root, repo_root_override=root),
                session=session,
                config=PicoConfig(mode="auto", context_limit_tokens=64_000),
            )
            self.assertIsNone(runtime.run.run_log)

            session.set_active_run("run_empty")
            runtime = Pico(
                ScriptedModel([]),
                Workspace.build(root, repo_root_override=root),
                session=session,
                config=PicoConfig(mode="auto", context_limit_tokens=64_000),
            )
            self.assertIsNone(runtime.run.run_log)
            self.assertEqual(session.active_run_id, "")

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
            results = [
                event
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result"
            ]
            self.assertEqual(
                [event.call_id for event in results],
                ["read-a", "read-b"],
            )

    def test_visible_assistant_text_is_durable_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n", encoding="utf-8")
            agent, _model = build_agent(
                root,
                [
                    AssistantTurn(
                        ModelAction.tool(
                            "read_file",
                            {"path": "subject.txt"},
                            call_id="read",
                        ),
                        "I will inspect subject.txt before deciding.",
                    ),
                    AssistantTurn(
                        ModelAction.final("The file contains alpha."),
                        "The inspection is complete.",
                    ),
                ],
            )

            outcome = agent.ask("Inspect subject.txt")
            messages = [
                AssistantTurn.from_dict(event.payload["turn"]).visible_text
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "assistant_turn"
                and AssistantTurn.from_dict(event.payload["turn"]).visible_text
            ]

            self.assertEqual(
                messages,
                [
                    "I will inspect subject.txt before deciding.",
                    "The inspection is complete.",
                ],
            )
            rendered = agent.run.run_log.history().render_projection()
            self.assertIn("I will inspect subject.txt before deciding.", rendered)
            self.assertIn("The inspection is complete.", rendered)

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
                for event in agent.read_run_events(outcome.run_id)
                if event.kind in {"tool_started", "tool_result"}
                and event.name != "read_file"
            ]
            self.assertEqual(
                transactions,
                [
                    ("tool_started", "edit"),
                    ("tool_result", "edit"),
                    ("tool_started", "write"),
                    ("tool_result", "write"),
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

    def test_context_overflow_without_compactor_does_not_request_again(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(Path(directory), [])
            with mock.patch.object(
                model,
                "complete_turn",
                side_effect=ProviderContextOverflow("window exceeded"),
            ) as request, mock.patch.object(
                model, "estimate_action_input_tokens", return_value=6000,
            ), self.assertRaisesRegex(
                SemanticCompactionError,
                "does not support isolated semantic compaction",
            ):
                agent.ask("Inspect")
            self.assertEqual(request.call_count, 1)
            self.assertTrue(agent.run.resumable)

    def test_context_overflow_without_compactable_history_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, model = build_agent(Path(directory), [])
            summarizer = mock.Mock()
            agent.prompt.semantic_summarizer = summarizer
            with mock.patch.object(model, "complete_turn", side_effect=ProviderContextOverflow("window exceeded")) as request, mock.patch.object(
                model, "estimate_action_input_tokens", return_value=6000,
            ), self.assertRaisesRegex(SemanticCompactionError, "summary did not reduce"):
                agent.ask("Inspect")
            self.assertEqual(request.call_count, 1)
            summarizer.summarize.assert_not_called()
            self.assertTrue(agent.run.resumable)

    def test_compaction_budgets_the_post_compaction_user_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _model = build_agent(root, [])
            RunLifecycle(agent).initialize("Inspect")
            old_guidance = "OLD-GUIDANCE:" + "G" * 150_000
            agent.run.run_log.append_user_guidance(old_guidance)
            for index in range(20):
                call = ToolCall(
                    "read_file",
                    {"path": f"file-{index}.txt"},
                    f"read-{index}",
                )
                agent.run.run_log.append("model_requested")
                agent.run.run_log.append_assistant_turn(
                    AssistantTurn(
                        ModelAction.tool(call.name, call.args, call_id=call.call_id)
                    )
                )
                agent.run.run_log.append_tool_result(
                    ToolOutcome(
                        call.call_id,
                        call.name,
                        "success",
                        "completed",
                        "none",
                        "X" * 2_000,
                    ),
                )
            summarizer = mock.Mock()
            summarizer.summarize.return_value = CompactedContext(
                constraints=("OLD-GUIDANCE summarized",),
                progress_done=(),
                progress_in_progress=(),
                progress_blocked=(),
                key_decisions=(),
                next_steps=(),
                critical_context=(),
            )
            agent.prompt.semantic_summarizer = summarizer

            prompt = agent.prompt.build_for_run(
                tool_surface=agent.tools.resolve_surface(),
                provider_context_tokens=agent.effective_context_limit_tokens,
            )

            self.assertIn("OLD-GUIDANCE summarized", prompt.input_text)
            self.assertNotIn(old_guidance, prompt.input_text)
            self.assertTrue(
                any(
                    event.kind == "compaction"
                    for event in agent.read_run_events(agent.run.projection.run_id)
                )
            )

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
            shell_started = next(
                event
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_started" and event.call_id == "test"
            )
            self.assertEqual(
                shell_started.payload["operation"],
                {
                    "command": check,
                    "cwd": ".",
                    "environment_policy": "minimal",
                    "shell": "/bin/sh",
                    "timeout_seconds": 120,
                },
            )
            shell_result = next(
                event for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result" and event.call_id == "test"
            ).payload["outcome"]
            self.assertEqual(shell_result["status"], "success")
            self.assertEqual(shell_result["side_effect_state"], "untracked")
            self.assertNotIn("effect_scope", shell_result)
            tool_events = [
                (event.kind, event.call_id)
                for event in agent.read_run_events(outcome.run_id)
                if event.kind in {"tool_started", "tool_result"}
            ]
            self.assertEqual(
                tool_events,
                [
                    ("tool_result", "read"),
                    ("tool_started", "edit"),
                    ("tool_result", "edit"),
                    ("tool_started", "test"),
                    ("tool_result", "test"),
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
                for event in agent.read_run_events(outcome.run_id)
                if event.kind in {"tool_started", "tool_result"}
            ]
            self.assertEqual(
                [event.kind for event in tool_events],
                ["tool_started", "tool_result"],
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
                kind="tool_exchange",
                timestamp="2026-09-12T00:00:00+00:00",
                payload={"outcome": {}},
            )

    def test_removed_model_instruction_event_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported Run Log kind"):
            RunEvent(
                event_id="run_old:event:000001",
                sequence=1,
                run_id="run_old",
                session_id="session_old",
                kind="model_instruction",
                timestamp="2026-09-12T00:00:00+00:00",
                payload={
                    "instruction": "old",
                    "evidence": "",
                    "evidence_artifact_id": "",
                },
            )

    def test_removed_compaction_without_file_activity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "compaction payload"):
            RunEvent(
                event_id="run_old:event:000001",
                sequence=1,
                run_id="run_old",
                session_id="session_old",
                kind="compaction",
                timestamp="2026-09-12T00:00:00+00:00",
                payload={
                    "content": "old summary",
                    "covered_event_ids": ["run_old:event:000000"],
                },
            )

    def test_removed_tool_outcome_effect_scope_is_rejected(self):
        value = ToolOutcome(
            "read",
            "read_file",
            "success",
            "completed",
            "none",
            "content",
        ).to_dict()
        value["effect_scope"] = "none"

        with self.assertRaisesRegex(ValueError, "invalid ToolOutcome"):
            ToolOutcome.from_dict(value)

    def test_removed_path_transitions_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "path_transitions"):
            ToolOutcome(
                "edit",
                "edit_file",
                "success",
                "completed",
                "changed",
                "edited",
                structured={
                    "path_transitions": [
                        {
                            "path": "subject.txt",
                            "before_state": "sha256:before",
                            "after_state": "sha256:after",
                        }
                    ]
                },
                affected_paths=("subject.txt",),
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
                for event in agent.read_run_events(outcome.run_id)
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
                for event in agent.read_run_events(outcome.run_id)
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

    def test_failed_started_persistence_prevents_the_write(self):
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
                "append_tool_started",
                side_effect=OSError("simulated started failure"),
            ), self.assertRaisesRegex(OSError, "started failure"):
                agent.ask("Replace alpha with beta")

            self.assertEqual(target.read_text(), "alpha\n")
            self.assertIsNone(agent.run.projection.pending_tool)

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

            def fail_edit_result(log, outcome, **kwargs):
                if outcome.tool_name == "edit_file":
                    raise OSError("simulated settlement failure")
                return real_append(log, outcome, **kwargs)

            with mock.patch.object(
                RunLog, "append_tool_result", new=fail_edit_result
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
            self.assertEqual(outcome.structured["path"], "subject.txt")
            self.assertTrue(outcome.structured["before_revision"].startswith("sha256:"))
            self.assertTrue(outcome.structured["after_revision"].startswith("sha256:"))

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
            real_append = RunLog.append_tool_result

            def lose_first_settlement(log, outcome, **kwargs):
                if outcome.tool_call_id == "interrupted-shell":
                    raise OSError("simulated settlement loss")
                return real_append(log, outcome, **kwargs)

            with mock.patch.object(
                RunLog,
                "append_tool_result",
                new=lose_first_settlement,
            ), self.assertRaisesRegex(OSError, "settlement loss"):
                agent.ask("Create an output and inspect it")

            outcome = agent.ask("Continue from the interrupted command")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual((root / "generated.txt").read_text(), "x")
            self.assertIn(
                "Workspace changes were not tracked",
                model.requests[1]["input_text"],
            )
            recovered = next(
                event
                for event in agent.read_run_events(outcome.run_id)
                if event.call_id == "interrupted-shell"
                and event.kind == "tool_result"
            )
            self.assertTrue(recovered.payload["recovered_from_interruption"])
            self.assertEqual(
                recovered.payload["outcome"]["side_effect_state"], "untracked"
            )
            self.assertIn(
                "Workspace changes were not tracked",
                recovered.payload["outcome"]["failure"]["detail"],
            )

    def test_compaction_cannot_split_call_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            run_store = store.runs(session.id)
            log = RunLog("run_compaction", session.id, run_store)
            log.append_user(TaskContract("Inspect", WriteScope("none")))
            call = ToolCall("write_file", {"path": "subject.txt", "content": "alpha"}, "write")
            log.append("model_requested")
            turn_event = log.append_assistant_turn(
                AssistantTurn(
                    ModelAction.tool(call.name, call.args, call_id=call.call_id)
                )
            )
            log.append_tool_started(
                call.call_id,
                effect_scope="workspace",
                potential_effects=[
                    {
                        "path": "subject.txt",
                        "before_state": "absent",
                    }
                ],
                operation={},
            )
            log.append_tool_result(
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
                log.append_compaction(
                    CompactedContext(
                        constraints=("summary",),
                        progress_done=(),
                        progress_in_progress=(),
                        progress_blocked=(),
                        key_decisions=(),
                        next_steps=(),
                        critical_context=(),
                        covered_through_sequence=turn_event.sequence,
                    )
                )


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
                event for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result" and event.name == "edit_file"
            )
            revision = settlement.payload["outcome"]["structured"]
            self.assertEqual(revision["path"], "subject.txt")
            self.assertTrue(revision["before_revision"].startswith("sha256:"))
            self.assertFalse(list(root.rglob("preimage_*")))
            failures = [
                event.payload["outcome"]["failure"]["code"]
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result"
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
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result"
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
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result"
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
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result" and event.name == "edit_file"
            ]
            self.assertEqual(
                [item["side_effect_state"] for item in settlements],
                ["none", "changed", "changed"],
            )
            self.assertFalse(list(root.rglob("preimage_*")))


if __name__ == "__main__":
    unittest.main()
