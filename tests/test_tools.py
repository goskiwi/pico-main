from pathlib import Path

import pytest

from pico.mutations import WorkspaceMutationService, file_revision
from pico.tool_context import ToolContext
from pico.tools import build_tool_registry, tool_patch_file, tool_read_file


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
        "list_files", "read_file", "search", "query_repo_map", "run_shell",
        "write_file", "patch_file", "memory_store", "memory_forget",
    }


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
