from pathlib import Path

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.contracts import ToolCall
from pico.mutations import WorkspaceMutationService, file_revision
from pico.tool_context import ToolContext
from pico.tools import build_tool_registry, tool_patch_file, tool_read_file, tool_search


def test_tool_context_supports_file_tools_without_full_pico(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
    )

    result = tool_read_file(context, {"path": "sample.txt", "start": 1, "end": 1})

    assert "# sample.txt" in result
    assert "revision: sha256:" in result
    assert "alpha" in result


def test_build_tool_registry_binds_runners_to_tool_context(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
    )

    tools = build_tool_registry(context)

    assert "read_file" in tools
    assert set(tools) == {
        "list_files", "read_file", "read_artifact", "search", "query_repo_map", "run_shell",
        "write_file", "patch_file", "memory_store", "memory_forget",
    }


def test_search_returns_workspace_relative_paths(tmp_path):
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_text("needle = 1\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    result = tool_search(context, {"pattern": "needle", "path": "src"})

    assert "src/demo.py:1:" in result
    assert str(tmp_path) not in result


def test_read_artifact_is_scoped_to_current_run_and_paginated(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )
    source = "".join(f"line-{index:04d} " + "x" * 80 + "\n" for index in range(300))
    agent.all_tools["list_files"]["run"] = lambda _args: source
    original = agent.run_tool(ToolCall("list_files", {"path": "."}, "call_source"))

    page = agent.run_tool(
        ToolCall(
            "read_artifact",
            {"artifact_id": original.artifact_id, "offset": 119 * 91, "max_bytes": 8192},
            "call_artifact_page",
        )
    )

    assert "bytes 10829-" in page.content
    assert "line-0119" in page.content
    assert "line-0179" in page.content
    assert "line-0118" not in page.content

    other = agent.artifact_store.write_tool_output("other-run", "call_other", "read_file", "secret\n")
    rejected = agent.run_tool(
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
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
        mutation_service=WorkspaceMutationService(tmp_path),
    )
    revision = file_revision(path)
    result = tool_patch_file(
        context,
        {"path": "sample.txt", "old_text": "alpha", "new_text": "beta", "expected_revision": revision},
    )
    assert "after_revision: sha256:" in result
    assert path.read_text(encoding="utf-8") == "beta\n"

    path.write_text("external\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="revision conflict"):
        tool_patch_file(
            context,
            {"path": "sample.txt", "old_text": "external", "new_text": "lost", "expected_revision": revision},
        )
