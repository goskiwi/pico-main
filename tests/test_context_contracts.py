import tempfile
import unittest
from pathlib import Path

from pico import (
    ModelAction,
    SessionStore,
    TaskContract,
    ToolCall,
    ToolOutcome,
    WriteScope,
    context_manager,
)
from pico.prompt_builder import _assemble_input
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
                for event in agent.run.run_log.events
                if event.kind == "tool_exchange"
            )
            self.assertEqual(first.payload["outcome"]["status"], "success")
            self.assertEqual(len(model.requests), 2)
            self.assertNotIn("deeper rules take precedence", model.requests[1]["instructions"])
            resets = [e for e in agent.read_run_events(outcome.run_id)
                      if e.kind == "provider_session_reset"]
            self.assertEqual(len(resets), 1)
            self.assertEqual(resets[0].payload["reason"], "repository_instructions_changed")


class ContextSelectionTests(unittest.TestCase):
    @staticmethod
    def _raw(history="", evidence=""):
        return {
            "runtime_policy": "runtime_policy: fixed",
            "repository_instructions": "",
            "task_request": "task_request: fixed",
            "runtime_instruction": "runtime_instruction: repair",
            "runtime_evidence": evidence,
            "latest_user_request": "latest_user_request: latest",
            "workspace": "Workspace: fixed",
            "history": history,
        }

    def test_bounded_history_keeps_a_complete_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            session = store.create(Path(directory))
            log = RunLog("run_context", session.id, store.runs(session.id))
            log.append_user(TaskContract("Inspect", WriteScope("none"), False))
            for call_id, path, content in (
                ("old", "old.txt", "old-result"),
                ("new", "new.txt", "new-result"),
            ):
                call = ToolCall("read_file", {"path": path}, call_id)
                log.append_tool_exchange(
                    call,
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
                540,
                section_caps={"workspace": 50},
                count_tokens=len,
                history=history,
                render_input=_assemble_input,
            )

            self.assertIn("new.txt", selected["history"])
            self.assertIn("new-result", selected["history"])
            self.assertNotIn("old.txt", selected["history"])
            rendered = _assemble_input(raw, selected)
            self.assertIn("task_request: fixed", rendered)
            self.assertIn("latest_user_request: latest", rendered)
            self.assertIn("runtime_instruction: repair", rendered)

    def test_required_runtime_evidence_over_budget_is_explicit(self):
        raw = self._raw(evidence="runtime_evidence: " + "x" * 500)

        with self.assertRaises(context_manager.ContextBudgetExceeded):
            context_manager.select_context(
                raw,
                100,
                section_caps={"workspace": 10},
                count_tokens=len,
                history=None,
                render_input=_assemble_input,
            )


if __name__ == "__main__":
    unittest.main()
