import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import (
    AssistantTurn,
    ModelAction,
    SessionStore,
    TaskContract,
    ToolCall,
    ToolOutcome,
    WriteScope,
    context_manager,
)
from pico.compaction_summary import CompactedContext
from pico.prompt_builder import _assemble_input, load_project_instructions
from pico.run_log import RunLog
from tests.support import build_agent


class RepositoryInstructionTests(unittest.TestCase):
    def test_root_rules_refresh_before_next_request_including_final(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("ROOT_RULE_V1\n", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "AGENTS.md").write_text("NESTED_RULE\n", encoding="utf-8")
            (nested / "subject.txt").write_text("alpha\n", encoding="utf-8")

            def update_rule_during_first_request(index):
                if index == 0:
                    (root / "AGENTS.md").write_text(
                        "ROOT_RULE_V2\n", encoding="utf-8"
                    )

            agent, model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "nested/subject.txt"}),
                    ModelAction.final("Read with current rules."),
                ],
                before_action=update_rule_during_first_request,
            )

            with mock.patch(
                "pico.prompt_builder.load_project_instructions",
                wraps=load_project_instructions,
            ) as load:
                outcome = agent.ask("Inspect the nested subject")

            self.assertEqual(outcome.status, "completed")
            self.assertIn("ROOT_RULE_V1", model.requests[0]["input_text"])
            self.assertNotIn("ROOT_RULE_V2", model.requests[0]["input_text"])
            self.assertIn("ROOT_RULE_V2", model.requests[1]["input_text"])
            self.assertTrue(
                all("NESTED_RULE" not in request["input_text"] for request in model.requests)
            )
            first = next(
                event
                for event in agent.read_run_events(outcome.run_id)
                if event.kind == "tool_result"
            )
            self.assertEqual(first.payload["outcome"]["status"], "success")
            self.assertEqual(len(model.requests), 2)
            self.assertEqual(load.call_count, 2)
            self.assertNotIn("deeper rules take precedence", model.requests[1]["instructions"])
            resets = [e for e in agent.read_run_events(outcome.run_id)
                      if e.kind == "provider_session_reset"]
            self.assertEqual(len(resets), 1)
            self.assertEqual(resets[0].payload["reason"], "project_instructions_changed")


class ContextSelectionTests(unittest.TestCase):
    @staticmethod
    def _raw(history="", guidance="retry_instruction: change strategy"):
        return {
            "permissions": "permissions: fixed",
            "project_instructions": "",
            "user_messages": 'user_messages: ["fixed"]',
            "retry_instruction": guidance,
            "workspace": "Workspace: fixed",
            "history": history,
        }

    def test_bounded_history_keeps_a_complete_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            log = RunLog("run_context", session.id, store.runs(session.id))
            log.append_user(TaskContract("Inspect", WriteScope("none")))
            for call_id, path, content in (
                ("old", "old.txt", "old-result"),
                ("new", "new.txt", "new-result"),
            ):
                call = ToolCall("read_file", {"path": path}, call_id)
                log.append("model_requested")
                log.append_assistant_turn(
                    AssistantTurn(
                        ModelAction.tool(call.name, call.args, call_id=call.call_id)
                    )
                )
                log.append_tool_result(
                    ToolOutcome(
                        call_id,
                        "read_file",
                        "success",
                        "completed",
                        "none",
                        content,
                    ),
                )
            history = log.history()
            full = history.render_projection()
            raw = self._raw(full)

            selected = context_manager.select_context(
                raw,
                500,
                section_caps={"workspace": 50},
                count_tokens=len,
                history=history,
                render_input=_assemble_input,
            )

            self.assertIn("new.txt", selected["history"])
            self.assertIn("new-result", selected["history"])
            self.assertNotIn("old.txt", selected["history"])
            rendered = _assemble_input(raw, selected)
            self.assertIn('user_messages: ["fixed"]', rendered)
            self.assertIn("retry_instruction: change strategy", rendered)
            self.assertLess(
                rendered.index("retry_instruction: change strategy"),
                rendered.index("<conversation_history>"),
            )

    def test_user_guidance_is_rendered_once_in_the_trusted_region(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            log = RunLog("run_guidance", session.id, store.runs(session.id))
            log.append_user(TaskContract("Change the service", WriteScope("none")))
            log.append_user_guidance("Do not modify database code")
            raw = self._raw("prior conversation")
            raw["user_messages"] = (
                'user_messages: ["Change the service", '
                '"Do not modify database code"]'
            )

            rendered = _assemble_input(
                raw,
                {
                    "user_messages": raw["user_messages"],
                    "history": raw["history"],
                },
            )

            self.assertEqual(rendered.count("Do not modify database code"), 1)
            self.assertLess(
                rendered.index("Do not modify database code"),
                rendered.index("<conversation_history>"),
            )

    def test_required_retry_guidance_over_budget_is_explicit(self):
        raw = self._raw(guidance="retry_instruction: " + "x" * 500)

        with self.assertRaises(context_manager.ContextBudgetExceeded):
            context_manager.select_context(
                raw,
                100,
                section_caps={"workspace": 10},
                count_tokens=len,
                history=None,
                render_input=_assemble_input,
            )

    def test_uncompacted_user_guidance_is_never_partially_selected(self):
        raw = self._raw(guidance="")
        raw["user_messages"] = "user_messages:\n" + "x" * 500

        with self.assertRaisesRegex(
            context_manager.ContextBudgetExceeded,
            "required Runtime context",
        ):
            context_manager.select_context(
                raw,
                100,
                section_caps={"workspace": 10},
                count_tokens=len,
                history=None,
                render_input=_assemble_input,
            )

    def test_committed_summary_is_reserved_before_optional_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            log = RunLog("run_required_summary", session.id, store.runs(session.id))
            log.append_user(TaskContract("Inspect", WriteScope("none")))
            old = log.append_user_guidance("older guidance")
            log.append_compaction(
                CompactedContext(
                    constraints=("S" * 180,),
                    progress_done=(),
                    progress_in_progress=(),
                    progress_blocked=(),
                    key_decisions=(),
                    next_steps=(),
                    critical_context=(),
                    covered_through_sequence=old.sequence,
                )
            )
            history = log.history()
            history_text = history.render_projection()
            raw = self._raw(history_text, guidance="")
            raw["workspace"] = "\n".join(["W"] * 40)
            summary_only_size = len(
                _assemble_input(
                    raw,
                    {
                        "user_messages": raw["user_messages"],
                        "history": history_text,
                    },
                )
            )

            selected = context_manager.select_context(
                raw,
                summary_only_size,
                section_caps={"workspace": 150},
                count_tokens=len,
                history=history,
                render_input=_assemble_input,
            )

            self.assertIn("S" * 180, selected["history"])
            self.assertNotIn("workspace", selected)

    def test_one_prompt_rebuild_reuses_one_history_snapshot_and_tool_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.txt").write_text("alpha\n")
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool("read_file", {"path": "subject.txt"}),
                    ModelAction.final("Read."),
                ],
            )

            with mock.patch.object(
                agent.prompt,
                "_history",
                wraps=agent.prompt._history,
            ) as history, mock.patch.object(
                agent.tools,
                "resolve_surface",
                wraps=agent.tools.resolve_surface,
            ) as surface:
                outcome = agent.ask("Read subject.txt")

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(history.call_count, 1)
            self.assertEqual(surface.call_count, 1)


if __name__ == "__main__":
    unittest.main()
