from pathlib import Path

import pico as mini_pkg
from pico.cli import build_welcome
from pico.runtime import Pico
from tests.helpers import build_agent


def test_runtime_does_not_expose_legacy_parse_api():
    assert not hasattr(Pico, "parse")


def test_short_read_summary_keeps_the_complete_file(tmp_path):
    agent = build_agent(tmp_path, [])
    result = "\n".join(f"{line}: value" for line in range(1, 13))

    summary = agent.summarize_tool_result("read_file", {"path": "small.py"}, result)

    assert "1: value" in summary
    assert "12: value" in summary
    assert "omitted" not in summary
    assert not hasattr(Pico, "parse_xml_tool")


def test_runtime_does_not_expose_legacy_security_api():
    legacy_names = [
        "redact_text",
        "redact_artifact",
        "looks_sensitive_env_name",
        "is_secret_env_name",
        "configured_secret_env_items",
        "detected_secret_env_items",
        "secret_env_summary",
        "detected_secret_env_summary",
        "shell_env",
    ]
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_memory_promotion_api():
    legacy_names = [
        "reject_durable_reason",
        "extract_durable_promotions",
        "promote_durable_memory",
        "llm_memory_index_text",
        "build_memory_extractor_prompt",
        "parse_memory_extractor_output",
        "llm_promote_durable_memory",
    ]
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_approval_api():
    assert not hasattr(Pico, "approve")


def test_runtime_does_not_expose_legacy_report_api():
    legacy_names = [
        "build_report",
        "record_tool_audit",
        "build_run_summary",
    ]
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_workspace_diff_api():
    legacy_names = [
        "capture_workspace_snapshot",
        "diff_workspace_snapshots",
    ]
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_runtime_does_not_expose_legacy_tool_policy_api():
    legacy_names = [
        "tool_capability",
        "tool_risk_level",
        "tool_permission_error",
        "dry_run_tool_result",
        "shell_policy_metadata",
        "shell_command_policy",
        "repeated_tool_call",
    ]
    assert not any(hasattr(Pico, name) for name in legacy_names)


def test_welcome_screen_keeps_box_shape_for_long_paths(tmp_path):
    deep = (
        tmp_path
        / "very"
        / "long"
        / "path"
        / "for"
        / "the"
        / "mini"
        / "agent"
        / "welcome"
        / "screen"
    )
    deep.mkdir(parents=True)
    agent = build_agent(deep, [])

    welcome = build_welcome(agent, model="qwen3.5:4b", host="http://127.0.0.1:11434")
    lines = welcome.splitlines()

    assert len(lines) >= 5
    assert len({len(line) for line in lines}) == 1
    assert "..." in welcome
    assert "(  o o  )" in welcome
    assert "MINI-CODING-AGENT" not in welcome
    assert "MINI CODING AGENT" not in welcome
    assert "pico" in welcome
    assert "local coding agent" in welcome
    assert "// READY" not in welcome
    assert "SLASH" not in welcome
    assert "READY      " not in welcome
    assert "commands: Commands:" not in welcome


def test_public_api_exports_resolve_through_package_path():
    assert mini_pkg.Pico is Pico
    assert mini_pkg.__all__ == ["Pico"]
    legacy_exports = [
        "build_welcome",
        "FakeModelClient",
        "MiniAgent",
        "DelegateOutcome",
        "DelegateScheduler",
        "OpenAICompatibleModelClient",
        "SessionStore",
        "WorkspaceContext",
    ]
    assert not any(hasattr(mini_pkg, name) for name in legacy_exports)
    assert Path(mini_pkg.__file__).as_posix().endswith("/pico/__init__.py")


def test_reviewer_skeleton_docs_exist():
    review_pack = Path("docs/review-pack/README.md")
    architecture = Path("docs/architecture/agent-harness-v1-overview.md")

    assert review_pack.exists()
    assert architecture.exists()

    review_text = review_pack.read_text(encoding="utf-8")
    assert "Project pitch" in review_text
    assert "Architecture map" in review_text
    assert "Benchmark evidence" in review_text
    assert "Sample run artifact list" in review_text

    architecture_text = architecture.read_text(encoding="utf-8")
    assert "Agent Harness v1" in architecture_text
    assert "task state" in architecture_text.lower()
