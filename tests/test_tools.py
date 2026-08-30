from pathlib import Path
from unittest.mock import patch

import pytest

import pico.mutations as mutation_module
from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.contracts import ToolCall, ToolRunnerResult
from pico.execution import ExecutionContext
from pico.mutations import WorkspaceMutationService, file_revision
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract
from pico.tool_context import ToolContext
from pico.tools import (
    build_action_tools,
    build_tool_registry,
    tool_edit_file,
    tool_list_files,
    tool_read_file,
    tool_search,
    validate_tool,
)


def test_native_function_schemas_are_strict(tmp_path):
    context = ToolContext(
        workspace_root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    for definition in build_action_tools(build_tool_registry(context)):
        schema = definition["parameters"]
        assert definition["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def start_run(agent):
    run_log = RunLog(
        "run_tool_test",
        "task_tool_test",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    first = run_log.append_user(
        TaskContract(
            goal="Exercise tools",
            task_kind="modify",
            requires_workspace_change=False,
            requires_verification=False,
        )
    )
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.run_log = run_log
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    return run_log


def run_active(agent, call):
    run_log = agent.run.run_log or start_run(agent)
    agent.apply_run_event(run_log.append_tool_call(call))
    return agent.tools.execute(call)


def test_tool_context_supports_file_tools_without_full_pico(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    context = ToolContext(
        workspace_root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
    )

    result = tool_read_file(context, {"path": "sample.txt", "start_line": 1, "end_line": 1})

    assert "# sample.txt" in result.content
    assert "revision: sha256:" in result.content
    assert "alpha" in result.content
    assert result.structured == {
        "path": "sample.txt",
        "start_line": 1,
        "end_line": 1,
        "total_lines": 2,
        "has_more": True,
        "revision": result.structured["revision"],
    }


def test_list_files_reports_when_the_result_is_truncated(tmp_path):
    for index in range(201):
        (tmp_path / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")
    context = ToolContext(
        workspace_root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    result = tool_list_files(context, {"path": "."})

    assert result.structured == {
        "path": ".",
        "returned_count": 200,
        "has_more": True,
    }


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
            {"path": "large.txt", "start_line": 1, "end_line": 1},
        )


def test_old_read_and_shell_parameter_names_are_rejected(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto"),
    )

    old_read = agent.tools.execute(
        "read_file",
        {"path": "README.md", "start": 1, "end": 1},
    )
    old_shell = agent.tools.execute(
        "run_shell",
        {"command": "true", "timeout": 20},
    )

    assert old_read.status == "rejected"
    assert old_read.failure.code == "invalid_arguments"
    assert old_shell.status == "rejected"
    assert old_shell.failure.code == "invalid_arguments"


def test_file_failures_have_typed_recovery_conditions(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("alpha\nalpha\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto"),
    )

    missing = agent.tools.execute(
        "read_file",
        {"path": "missing.py", "start_line": 1, "end_line": 1},
    )
    repeated_missing = agent.tools.execute(
        "read_file",
        {"path": "missing.py", "start_line": 1, "end_line": 1},
    )
    revision = file_revision(target)
    not_found = run_active(
        agent,
        ToolCall(
            "edit_file",
            {"path": "README.md", "old_text": "missing", "new_text": "beta", "expected_revision": revision},
            "call_not_found",
        ),
    )
    ambiguous = run_active(
        agent,
        ToolCall(
            "edit_file",
            {"path": "README.md", "old_text": "alpha", "new_text": "beta", "expected_revision": revision},
            "call_ambiguous",
        ),
    )
    target.write_text("external\n", encoding="utf-8")
    conflict = run_active(
        agent,
        ToolCall(
            "edit_file",
            {"path": "README.md", "old_text": "external", "new_text": "beta", "expected_revision": revision},
            "call_conflict",
        ),
    )

    assert (missing.failure.code, missing.failure.recovery) == (
        "missing_path",
        "retry_after_change",
    )
    assert missing.correction_action == "repair"
    assert repeated_missing.failure.code == "missing_path"
    assert repeated_missing.correction_action == "repair"
    assert (not_found.failure.code, not_found.failure.recovery) == (
        "text_not_found",
        "retry_after_change",
    )
    assert not_found.structured == {
        "path": "README.md",
        "actual_revision": revision,
        "match_count": 0,
        "recommended_next_tool": "read_file",
    }
    assert (ambiguous.failure.code, ambiguous.failure.recovery) == (
        "ambiguous_text_match",
        "retry_after_change",
    )
    assert ambiguous.structured["match_count"] == 2
    assert (conflict.failure.code, conflict.failure.recovery) == (
        "revision_conflict",
        "retry_after_change",
    )
    assert conflict.structured == {
        "path": "README.md",
        "expected_revision": revision,
        "actual_revision": file_revision(target),
        "recommended_next_tool": "read_file",
    }

    (tmp_path / "missing.py").write_text("created\n", encoding="utf-8")
    after_change = run_active(
        agent,
        ToolCall(
            "read_file",
            {"path": "missing.py", "start_line": 1, "end_line": 1},
            "call_after_change",
        ),
    )
    assert after_change.status == "success"


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
    assert result.structured["match_count"] == 200
    assert result.structured["truncated"] is True


def test_fallback_search_uses_regex_and_classifies_invalid_patterns(tmp_path):
    (tmp_path / "source.txt").write_text("Needle value\n", encoding="utf-8")
    context = ToolContext(
        workspace_root=tmp_path.resolve(),
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=dict,
    )

    with patch("pico.tools.shutil.which", return_value=None):
        matched = tool_search(context, {"pattern": "N.edle", "path": "."})
        invalid = tool_search(context, {"pattern": "[", "path": "."})

    assert "source.txt:1:" in matched.content
    assert matched.structured["engine"] == "python_regex"
    assert invalid.failure.code == "invalid_search_pattern"


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
    original = run_active(
        agent,
        ToolCall("list_files", {"path": "."}, "call_source")
    )

    artifact_path = (
        tmp_path
        / ".pico"
        / "runs"
        / "run_tool_test"
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
        page = run_active(
            agent,
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
    rejected = run_active(
        agent,
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
    assert result.structured["replacement_count"] == 1
    assert result.structured["changed"] is True
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
        "pico.mutations.atomic_replace_bytes",
        lambda target, payload, **options: calls.append(
            (target, payload, options)
        ),
    )
    revision = file_revision(path)

    receipt = service.edit(path, "same", "same", revision)
    assert receipt.before_revision == receipt.after_revision == revision
    assert receipt.diff == ""
    assert calls == []


def test_write_revalidates_revision_at_atomic_commit(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    service = WorkspaceMutationService(tmp_path)
    original_replace = mutation_module.atomic_replace_bytes

    def drift_after_staging(target, payload, **options):
        original_guard = options["commit_guard"]

        def guarded_commit():
            path.write_text("external-change\n", encoding="utf-8")
            original_guard()

        return original_replace(
            target,
            payload,
            mode=options["mode"],
            commit_guard=guarded_commit,
        )

    monkeypatch.setattr(
        "pico.mutations.atomic_replace_bytes",
        drift_after_staging,
    )

    with pytest.raises(RuntimeError, match="revision conflict") as failure:
        service.write(path, "agent-change\n")

    assert failure.value.structured["expected_revision"] == "absent"
    assert failure.value.structured["actual_revision"] == file_revision(path)
    assert path.read_text(encoding="utf-8") == "external-change\n"
    assert list(tmp_path.glob("sample.txt.*.tmp")) == []


def test_edit_revalidates_revision_at_atomic_commit(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    path.write_text("alpha\n", encoding="utf-8")
    service = WorkspaceMutationService(tmp_path)
    revision = file_revision(path)
    original_replace = mutation_module.atomic_replace_bytes

    def drift_after_staging(target, payload, **options):
        original_guard = options["commit_guard"]

        def guarded_commit():
            path.write_text("external-change\n", encoding="utf-8")
            original_guard()

        return original_replace(
            target,
            payload,
            mode=options["mode"],
            commit_guard=guarded_commit,
        )

    monkeypatch.setattr(
        "pico.mutations.atomic_replace_bytes",
        drift_after_staging,
    )

    with pytest.raises(RuntimeError, match="revision conflict"):
        service.edit(path, "alpha", "beta", revision)

    assert path.read_text(encoding="utf-8") == "external-change\n"
    assert list(tmp_path.glob("sample.txt.*.tmp")) == []
