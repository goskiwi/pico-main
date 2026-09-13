import tempfile
import unittest
from pathlib import Path

from pico import Pico, PicoConfig, SessionStore, Workspace
from tests.support import ScriptedModel


class ContextBudgetTests(unittest.TestCase):
    @staticmethod
    def _agent(root, model, config):
        workspace = Workspace.build(root, repo_root_override=root)
        session = SessionStore(root / ".pico" / "sessions").create(workspace.root)
        return Pico(model, workspace, session, config=config)

    def test_runtime_limit_is_clamped_by_model_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = ScriptedModel([])
            model.context_window_tokens = 128_000

            agent = self._agent(root, model, PicoConfig())

            self.assertEqual(agent.context_limit_tokens, 128_000)
            self.assertEqual(agent.config.max_output_tokens, 32_000)
            self.assertEqual(agent.config.recent_history_tokens, 20_000)

    def test_runtime_limit_may_be_lower_than_model_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = ScriptedModel([])
            model.context_window_tokens = 1_000_000

            agent = self._agent(
                root,
                model,
                PicoConfig(context_limit_tokens=128_000),
            )

            self.assertEqual(agent.context_limit_tokens, 128_000)

    def test_effective_model_window_must_fit_output_and_recent_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = ScriptedModel([])
            model.context_window_tokens = 32_000

            with self.assertRaisesRegex(ValueError, "exceed max_output_tokens"):
                self._agent(root, model, PicoConfig())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = ScriptedModel([])
            model.context_window_tokens = 40_000

            with self.assertRaisesRegex(ValueError, "recent history"):
                self._agent(root, model, PicoConfig())

    def test_invalid_limits_and_removed_options_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            PicoConfig(context_limit_tokens=0)
        with self.assertRaisesRegex(ValueError, "exceed max_output_tokens"):
            PicoConfig(context_limit_tokens=32_000)
        with self.assertRaisesRegex(ValueError, "recent history"):
            PicoConfig(context_limit_tokens=40_000)
        for removed in (
            {"max_new_tokens": 1_000},
            {"turn_timeout_seconds": 60},
            {"context_budget_tokens": 128_000},
            {"model_context_window_tokens": 128_000},
            {"compaction_reserve_tokens": 32_000},
            {"compaction_keep_recent_tokens": 20_000},
            {"summary_max_output_tokens": 16_000},
            {"provider_context_limit_tokens": 128_000},
            {"verification_command": "pytest -q"},
            {"verification_required": True},
        ):
            with self.assertRaises(TypeError):
                PicoConfig(**removed)
