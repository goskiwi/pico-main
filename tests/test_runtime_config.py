from pico import PicoConfig
from pico.cli import build_arg_parser
from pico.runtime_config import DEFAULT_SHELL_ENV_ALLOWLIST


def test_default_and_explicit_empty_shell_allowlists_are_distinct():
    assert PicoConfig().normalized().shell_env_allowlist == DEFAULT_SHELL_ENV_ALLOWLIST
    assert PicoConfig(shell_env_allowlist=()).normalized().shell_env_allowlist == ()


def test_runtime_default_max_new_tokens_matches_cli():
    config = PicoConfig.build()
    args = build_arg_parser().parse_args([])

    assert config.max_new_tokens == args.max_new_tokens == 1024


def test_cli_accepts_custom_max_new_tokens():
    args = build_arg_parser().parse_args(
        [
            "--max-new-tokens",
            "2048",
            "--compaction-reserve-tokens",
            "12000",
            "--compaction-keep-recent-tokens",
            "16000",
        ]
    )

    assert args.max_new_tokens == 2048
    assert args.compaction_reserve_tokens == 12000
    assert args.compaction_keep_recent_tokens == 16000


def test_provider_context_limit_always_exceeds_output_reserve():
    config = PicoConfig.build(
        max_new_tokens=2048,
        provider_context_limit_tokens=1024,
    )

    assert config.provider_context_limit_tokens == 2049


def test_compaction_budgets_scale_down_for_small_context_windows():
    config = PicoConfig.build(provider_context_limit_tokens=8000)

    assert config.compaction_reserve_tokens == 2000
    assert config.compaction_keep_recent_tokens == 6000


def test_removed_memory_feature_flags_are_rejected():
    try:
        PicoConfig.build(feature_flags={"memory": True})
    except ValueError as exc:
        assert str(exc) == "unknown feature flags: memory"
    else:
        raise AssertionError("removed feature flag was accepted")
