import pytest

from pico import PicoConfig
from pico.cli import build_arg_parser
from pico.runtime_config import DEFAULT_SHELL_ENV_ALLOWLIST


def test_default_and_explicit_empty_shell_allowlists_are_distinct():
    assert PicoConfig().normalized().shell_env_allowlist == DEFAULT_SHELL_ENV_ALLOWLIST
    assert PicoConfig(shell_env_allowlist=()).normalized().shell_env_allowlist == ()


def test_runtime_context_defaults_match_cli():
    config = PicoConfig.build()
    args = build_arg_parser().parse_args([])

    assert config.max_new_tokens == args.max_new_tokens == 1024
    assert (
        config.provider_context_limit_tokens
        == args.provider_context_limit
        == 272_000
    )
    assert config.compaction_reserve_tokens == args.compaction_reserve_tokens == 16_384
    assert (
        config.compaction_keep_recent_tokens
        == args.compaction_keep_recent_tokens
        == 20_000
    )


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


def test_provider_context_limit_must_exceed_output_reserve():
    with pytest.raises(
        ValueError,
        match="provider context limit must exceed max_new_tokens",
    ):
        PicoConfig.build(
            max_new_tokens=2048,
            provider_context_limit_tokens=1024,
        )


def test_small_context_windows_require_explicit_compaction_budgets():
    with pytest.raises(
        ValueError,
        match="compaction reserve must be smaller",
    ):
        PicoConfig.build(provider_context_limit_tokens=8000)

    config = PicoConfig.build(
        provider_context_limit_tokens=8000,
        compaction_reserve_tokens=2000,
        compaction_keep_recent_tokens=6000,
    )

    assert config.compaction_reserve_tokens == 2000
    assert config.compaction_keep_recent_tokens == 6000


def test_valid_custom_reserve_is_not_clamped_to_a_context_ratio():
    config = PicoConfig.build(
        provider_context_limit_tokens=64_000,
        compaction_reserve_tokens=16_384,
    )

    assert config.compaction_reserve_tokens == 16_384


def test_feature_flags_are_not_a_runtime_configuration_surface():
    with pytest.raises(TypeError, match="unknown Pico configuration: feature_flags"):
        PicoConfig.build(feature_flags={"context_reduction": False})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_new_tokens": 0}, "max_new_tokens must be positive"),
        (
            {"max_tool_executions": 0},
            "max_tool_executions must be positive",
        ),
        ({"run_timeout_seconds": 0}, "run_timeout_seconds must be positive"),
        ({"subagent_max_workers": 0}, "subagent_max_workers must be between"),
        ({"subagent_max_workers": 4}, "subagent_max_workers must be between"),
    ],
)
def test_runtime_limits_reject_invalid_values_instead_of_clamping(
    overrides,
    message,
):
    with pytest.raises(ValueError, match=message):
        PicoConfig.build(**overrides)
