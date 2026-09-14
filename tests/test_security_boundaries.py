import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico import (
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    TaskContract,
    ToolOutcome,
    Workspace,
    WriteScope,
)
from pico.run_log import RunLog
from pico.run_store import RunStore
from tests.support import ScriptedModel, build_agent, request_text


def build_policy_agent(root, actions, *, mode, allowed_write_paths=None):
    workspace = Workspace.build(root, repo_root_override=root)
    model = ScriptedModel(actions)
    session = SessionStore(root / ".pico" / "sessions").create(workspace.root)
    agent = Pico(
        model,
        workspace,
        session=session,
        config=PicoConfig(
            mode=mode,
            allowed_write_paths=allowed_write_paths,
            context_limit_tokens=32_000,
            recent_history_tokens=2_000,
            max_output_tokens=1_000,
        ),
        approval_handler=lambda _name, _args, _plan: True,
    )
    return agent


class SecurityBoundaryTests(unittest.TestCase):
    def test_directory_traversal_and_runtime_directories_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace.build(root, repo_root_override=root)

            for path in (
                "../outside.txt",
                ".git/config",
                ".GIT/config",
                ".pico/events.jsonl",
                ".PiCo/events.jsonl",
            ):
                with self.assertRaises(ValueError):
                    workspace.resolve_tool_path(path)

    def test_symlink_cannot_redirect_a_tool_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "secret.txt"
            external.write_text("secret\n", encoding="utf-8")
            (root / "redirect.txt").symlink_to(external)
            (root / "redirect-directory").symlink_to(Path(outside))
            workspace = Workspace.build(root, repo_root_override=root)

            for path in ("redirect.txt", "redirect-directory/secret.txt"):
                with self.assertRaisesRegex(ValueError, "escapes workspace"):
                    workspace.resolve_tool_path(path)

    def test_ask_mode_and_allowed_paths_are_enforced_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ask_agent = build_policy_agent(
                root,
                [
                    ModelAction.tool(
                        "write_file",
                        {"path": "ask-denied.txt", "content": "denied\n"},
                        call_id="ask-write",
                    ),
                    ModelAction.final("The write was denied."),
                ],
                mode="ask",
            )

            ask_outcome = ask_agent.ask("Try to create a file")

            self.assertFalse((root / "ask-denied.txt").exists())
            ask_result = next(
                ToolOutcome.from_dict(event.payload["outcome"])
                for event in ask_agent.read_run_events(ask_outcome.run_id)
                if event.kind == "tool_result"
            )
            self.assertEqual(ask_result.failure.code, "tool_not_allowed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scoped_agent = build_policy_agent(
                root,
                [
                    ModelAction.tool(
                        "write_file",
                        {"path": "outside.txt", "content": "denied\n"},
                        call_id="outside-write",
                    ),
                    ModelAction.tool(
                        "write_file",
                        {"path": "allowed.txt", "content": "allowed\n"},
                        call_id="allowed-write",
                    ),
                    ModelAction.final("Only the allowed file was created."),
                ],
                mode="auto",
                allowed_write_paths=("allowed.txt",),
            )

            scoped_outcome = scoped_agent.ask("Create only allowed.txt")

            self.assertFalse((root / "outside.txt").exists())
            self.assertEqual((root / "allowed.txt").read_text(), "allowed\n")
            outcomes = [
                ToolOutcome.from_dict(event.payload["outcome"])
                for event in scoped_agent.read_run_events(scoped_outcome.run_id)
                if event.kind == "tool_result"
            ]
            self.assertEqual(outcomes[0].failure.code, "write_scope_denied")
            self.assertEqual(outcomes[1].status, "success")

    def test_persisted_goal_call_and_operation_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"PICO_OPENAI_API_KEY": "test-secret-value"},
            clear=False,
        ):
            root = Path(directory)
            agent, _model = build_agent(
                root,
                [
                    ModelAction.tool(
                        "run_shell",
                        {"command": "printf test-secret-value"},
                        call_id="shell",
                    ),
                    ModelAction.final("Done."),
                ],
            )

            outcome = agent.ask("Use test-secret-value only for this test")
            persisted = json.dumps(
                [event.to_dict() for event in agent.read_run_events(outcome.run_id)]
            )

            self.assertNotIn("test-secret-value", persisted)
            self.assertIn("<redacted>", persisted)

    def test_repository_instructions_and_final_answer_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"PICO_OPENAI_API_KEY": "test-secret-value"},
            clear=False,
        ):
            root = Path(directory)
            (root / "AGENTS.md").write_text(
                "Never print test-secret-value.\n",
                encoding="utf-8",
            )
            agent, model = build_agent(
                root,
                [ModelAction.final("Returned test-secret-value")],
            )

            outcome = agent.ask("Inspect")
            persisted = json.dumps(
                [event.to_dict() for event in agent.read_run_events(outcome.run_id)]
            )

            self.assertNotIn("test-secret-value", request_text(model.requests[0]))
            self.assertNotIn("test-secret-value", persisted)
            self.assertNotIn("test-secret-value", outcome.answer)
            self.assertIn("<redacted>", outcome.answer)

    def test_environment_files_are_protected_but_examples_are_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("SECRET=value\n")
            (root / ".env.local").write_text("SECRET=value\n")
            (root / ".env.production").write_text("SECRET=value\n")
            (root / ".env.test.local").write_text("SECRET=value\n")
            (root / ".ENV").write_text("SECRET=value\n")
            protected_directory = root / ".Env.Secrets"
            protected_directory.mkdir()
            (protected_directory / "secret.txt").write_text("SECRET=value\n")
            example = root / ".env.example"
            example.write_text("SECRET=example\n")
            sample = root / ".env.sample"
            sample.write_text("SECRET=sample\n")
            workspace = Workspace.build(root, repo_root_override=root)

            for name in (
                ".env",
                ".env.local",
                ".env.production",
                ".env.test.local",
                ".ENV",
                ".Env.Secrets/secret.txt",
            ):
                with self.assertRaisesRegex(ValueError, "protected environment"):
                    workspace.resolve_tool_path(name)
            self.assertEqual(workspace.resolve_tool_path(".env.example"), example.resolve())
            self.assertEqual(workspace.resolve_tool_path(".env.sample"), sample.resolve())

    def test_event_log_is_created_private_even_with_permissive_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs")
            log = RunLog("run_private", "session_private", store)
            previous = os.umask(0o022)
            try:
                log.append_user(
                    TaskContract(
                        goal="Inspect",
                        mode="ask",
                        allowed_tools=("read_file",),
                        write_scope=WriteScope("none"),
                    )
                )
            finally:
                os.umask(previous)

            mode = stat.S_IMODE(store.events_path(log.run_id).stat().st_mode)
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
