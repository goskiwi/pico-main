import unittest

from pico.config import PicoConfig


class ContextBudgetTests(unittest.TestCase):
    def test_default_budget_is_smaller_than_declared_model_window(self):
        config = PicoConfig(model_context_window_tokens=1_000_000)
        self.assertEqual(config.context_budget_tokens, 272_000)
        self.assertEqual(config.context_budget_tokens - config.compaction_reserve_tokens, 240_000)
        self.assertEqual(config.compaction_keep_recent_tokens, 20_000)

    def test_smaller_model_requires_a_smaller_runtime_budget(self):
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            PicoConfig(model_context_window_tokens=128_000)
        config = PicoConfig(model_context_window_tokens=128_000, context_budget_tokens=128_000)
        self.assertEqual(config.context_budget_tokens - config.compaction_reserve_tokens, 96_000)

    def test_invalid_window_and_removed_option_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            PicoConfig(model_context_window_tokens=0)
        with self.assertRaises(TypeError):
            PicoConfig(provider_context_limit_tokens=128_000)
