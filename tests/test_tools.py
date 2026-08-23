from pathlib import Path
from unittest.mock import patch

import pytest

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.contracts import ToolCall, ToolRunnerResult
from pico.mutations import WorkspaceMutationService, file_revision
from pico.tool_context import ToolContext
from pico.tools import (
    build_tool_registry,
    tool_edit_file,
    tool_read_file,
    tool_search,
    validate_tool,
)


def test_tool_context_supports_file_tools_without_full_pico(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    context = ToolContext(
        workspace_root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
    )

    result = tool_read_file(context, {"path": "sample.txt", "start": 1, "end": 1})

    assert "# sample.txt" in result.content
    assert "revision: sha256:" in result.content
    assert "alpha" in result.content


def test_read_file_rejects_files_over_the_host_read_limit(tmp_path):
    (tmp_path / "large.txt").write_text("12345", encoding="utf-8")
    context = ToolContext(
        workspace_root=tmp_path.resolve(),
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    with (
        patch("pico.tools.READ_FILE_MAX_BYTES", 4),
        pytest.raises(ValueError, match="exceeds 4 bytes"),
    ):
        validate_tool(
            context,
            "read_file",
            {"path": "large.txt", "start": 1, "end": 1},
        )


def test_build_tool_registry_binds_runners_to_tool_context(tmp_path):
    context = ToolContext(
        workspace_root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
    )

    tools = build_tool_registry(context)

    assert "read_file" in tools
    assert set(tools) == {
        "list_files", "read_file", "read_artifact", "search", "run_shell",
        "write_file", "edit_file", "update_working_state", "memory_recall",
        "memory_store", "memory_forget",
    }
    assert "&&" in tools["run_shell"]["description"]
    assert "as small as possible" in tools["edit_file"]["description"]


def test_search_returns_workspace_relative_paths(tmp_path):
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_text("needle = 1\n", encoding="utf-8")
    context = ToolContext(
        workspace_root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    result = tool_search(context, {"pattern": "needle", "path": "src"})

    assert "src/demo.py:1:" in result.content
    assert str(tmp_path) not in result.content


def test_fallback_search_skips_symlinks_that_leave_workspace(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("EXTERNAL_SENTINEL\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    context = ToolContext(
        workspace_root=tmp_path.resolve(),
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    with patch("pico.tools.shutil.which", return_value=None):
        result = tool_search(
            context, {"pattern": "EXTERNAL_SENTINEL", "path": "."}
        )

    assert "EXTERNAL_SENTINEL" not in result.content


def test_fallback_search_has_a_global_match_limit(tmp_path):
    (tmp_path / "many.txt").write_text(
        "".join(f"needle {index}\n" for index in range(500)), encoding="utf-8"
    )
    context = ToolContext(
        workspace_root=tmp_path.resolve(),
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    with patch("pico.tools.shutil.which", return_value=None):
        result = tool_search(context, {"pattern": "needle", "path": "."})

    matches = [line for line in result.content.splitlines() if "needle" in line]
    assert len(matches) == 200
    assert "search result limit reached" in result.content


def test_read_artifact_is_scoped_to_current_run_and_paginated(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto"),
    )
    source = "".join(f"line-{index:04d} " + "x" * 80 + "\n" for index in range(300))
    agent.tools.registry["list_files"]["run"] = lambda _args: ToolRunnerResult(source)
    original = agent.tools.run(
        ToolCall("list_files", {"path": "."}, "call_source")
    )

    artifact_path = (
        tmp_path
        / ".pico"
        / "runs"
        / "manual"
        / "artifacts"
        / f"{original.artifact_id}.txt"
    )
    content_reads = []
    original_read_bytes = Path.read_bytes

    def counted_read_bytes(path):
        if path == artifact_path:
            content_reads.append(path)
        return original_read_bytes(path)

    with patch.object(Path, "read_bytes", counted_read_bytes):
        page = agent.tools.run(
            ToolCall(
                "read_artifact",
                {
                    "artifact_id": original.artifact_id,
                    "offset": 119 * 91,
                    "max_bytes": 8192,
                },
                "call_artifact_page",
            )
        )

    assert content_reads == [artifact_path]
    assert "bytes 10829-" in page.content
    assert "line-0119" in page.content
    assert "line-0179" in page.content
    assert "line-0118" not in page.content

    other = agent.dependencies.artifacts.write_tool_output(
        "other-run", "call_other", "secret\n"
    )
    rejected = agent.tools.run(
        ToolCall(
            "read_artifact",
            {"artifact_id": other["artifact_id"], "offset": 0, "max_bytes": 8192},
            "call_cross_run",
        )
    )
    assert rejected.status == "error"
    assert "missing" in rejected.failure.detail


def test_patch_is_revision_bound_and_atomic(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("alpha\n", encoding="utf-8")
    context = ToolContext(
        workspace_root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
        mutation_service=WorkspaceMutationService(tmp_path),
    )
    revision = file_revision(path)
    result = tool_edit_file(
        context,
        {"path": "sample.txt", "old_text": "alpha", "new_text": "beta", "expected_revision": revision},
    )
    assert "after_revision: sha256:" in result.content
    assert path.read_text(encoding="utf-8") == "beta\n"

    path.write_text("external\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="revision conflict"):
        tool_edit_file(
            context,
            {"path": "sample.txt", "old_text": "external", "new_text": "lost", "expected_revision": revision},
        )


def test_noop_mutations_do_not_replace_the_file(tmp_path, monkeypatch):
    path = tmp_path / "same.txt"
    path.write_text("same\n", encoding="utf-8")
    service = WorkspaceMutationService(tmp_path)
    calls = []
    monkeypatch.setattr(
        service,
        "_atomic_replace",
        lambda target, payload: calls.append((target, payload)),
    )
    revision = file_revision(path)

    assert service.write(path, "same\n", revision) == (revision, revision)
    assert service.edit(path, "same", "same", revision) == (revision, revision)
    assert calls == []
