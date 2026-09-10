"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import time
from functools import partial
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .command_runner import shell_argv
from .contracts import (
    TOOL_ARTIFACT_ID_PATTERN,
    TOOL_OUTPUT_MAX_BYTES,
    FailureInfo,
    ToolExecutionPlan,
    ToolFailureError,
    ToolOutcome,
    ToolRunnerResult,
)
from .verification import (
    RepositorySnapshotError,
    capture_repository_state,
    repository_state_changes,
)
from .workspace import IGNORED_PATH_NAMES

READ_FILE_MAX_OUTPUT_BYTES = 512 * 1024
READ_FILE_MAX_LINES = 2000
SEARCH_MAX_MATCHES = 200
SEARCH_MAX_OUTPUT_BYTES = 512 * 1024
SEARCH_TIMEOUT_SECONDS = 10.0
RUN_COMMAND_TIMEOUT_SECONDS = 120


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListFilesArgs(ToolArgs):
    path: str = Field(default=".", description="Path relative to workspace root, not startup directory; '.' is workspace root.")
    offset: int = Field(default=0, ge=0, description="Entry offset; use next_offset to continue. Restart at zero if the directory changes.")
    limit: int = Field(default=200, ge=1, le=200)


class ReadFileArgs(ToolArgs):
    path: str = Field(min_length=1, description="File path relative to workspace root, not startup directory.")
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)


class ReadArtifactArgs(ToolArgs):
    artifact_id: str = Field(pattern=TOOL_ARTIFACT_ID_PATTERN)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=8192, ge=4, le=8192)


class SearchArgs(ToolArgs):
    pattern: str = Field(min_length=1)
    path: str = Field(default=".", description="Path relative to workspace root, not startup directory; '.' is workspace root.")


class RunShellArgs(ToolArgs):
    command: str = Field(min_length=1)


class WriteFileArgs(ToolArgs):
    path: str = Field(min_length=1, description="File path relative to workspace root, not startup directory.")
    content: str


class EditFileArgs(ToolArgs):
    path: str = Field(min_length=1, description="File path relative to workspace root, not startup directory.")
    old_text: str = Field(min_length=1)
    new_text: str


class SubmitFinalArgs(ToolArgs):
    answer: str = Field(min_length=1)


def function_schema(args_schema: type[BaseModel]) -> dict[str, Any]:
    schema = args_schema.model_json_schema()
    schema.pop("title", None)

    def enforce_strict_objects(value):
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["additionalProperties"] = False
                # Responses strict mode requires every property at every nested
                # object level. Runtime defaults still apply to direct calls.
                value["required"] = list(properties)
            for item in value.values():
                enforce_strict_objects(item)
        elif isinstance(value, list):
            for item in value:
                enforce_strict_objects(item)

    enforce_strict_objects(schema)
    return schema


def build_action_tools(tools):
    definitions = []
    for name, tool in tools.items():
        definitions.append(
            {
                "type": "function",
                "name": name,
                "description": tool["description"],
                "parameters": function_schema(tool["args_schema"]),
                "strict": True,
            }
        )
    definitions.append(
        {
            "type": "function",
            "name": "submit_final",
            "description": "Return the final answer after the task is complete.",
            "parameters": function_schema(SubmitFinalArgs),
            "strict": True,
        }
    )
    return definitions


def _validate_list_files(context, args, *, path_resolver):
    path = path_resolver(args.get("path", "."))
    if not path.exists():
        raise ToolFailureError(
            "missing_path", f"path does not exist: {args.get('path', '.')}"
        )
    if not path.is_dir():
        raise ToolFailureError("invalid_path_type", "path is not a directory")
    return args


def _validate_read_file(context, args, *, path_resolver, workspace_root):
    path = path_resolver(args["path"])
    if not path.exists():
        raise ToolFailureError("missing_path", f"path does not exist: {args['path']}",
                               structured={"path": path.relative_to(workspace_root).as_posix(), "revision": "absent"})
    if not path.is_file():
        raise ToolFailureError("invalid_path_type", "path is not a file")
    if int(args.get("end_line", 200)) < int(args.get("start_line", 1)):
        raise ValueError("invalid line range")
    if (
        int(args.get("end_line", 200)) - int(args.get("start_line", 1)) + 1
        > READ_FILE_MAX_LINES
    ):
        raise ValueError(f"read_file returns at most {READ_FILE_MAX_LINES} lines")
    return args


def _validate_read_artifact(context, args, *, artifact_store):
    if artifact_store is None or not context.run_id:
        raise ValueError("artifact store is unavailable")
    return args


def _validate_search(context, args, *, path_resolver):
    if not str(args.get("pattern", "")).strip():
        raise ValueError("pattern must not be empty")
    path = path_resolver(args.get("path", "."))
    if not path.exists():
        raise ToolFailureError(
            "missing_path", f"path does not exist: {args.get('path', '.')}"
        )
    return args


def _require_mutation_service(mutation_service):
    if mutation_service is None:
        raise ValueError("workspace mutation service is unavailable")


def _validate_write_file(context, args, *, mutation_service, workspace_root):
    _logical, path = context.execution_plan.paths[0]
    if path.exists():
        if path.is_dir():
            raise ToolFailureError("invalid_path_type", "path is a directory")
        raise ToolFailureError(
            "existing_file_requires_edit",
            "write_file only creates new files; read the current file and use edit_file",
            structured={
                "path": path.relative_to(workspace_root).as_posix(),
                "recommended_next_tool": "read_file",
            },
        )
    _require_mutation_service(mutation_service)
    return args


def _validate_edit_file(context, args, *, mutation_service):
    # Edit admission is intentionally strict so the later mutation is
    # deterministic and revision-bound.
    _logical, path = context.execution_plan.paths[0]
    if not path.exists():
        raise ToolFailureError("missing_path", f"path does not exist: {args['path']}")
    if not path.is_file():
        raise ToolFailureError("invalid_path_type", "path is not a file")
    _require_mutation_service(mutation_service)
    return args


def _validate_run_shell(context, args, *, command_runner):
    command = str(args["command"]).strip()
    if not command:
        raise ValueError("run_shell requires a non-blank command")
    if command_runner is None:
        raise RuntimeError("run_shell requires a CommandRunner")
    return {"command": command}


def tool_list_files(context, args, *, path_resolver, workspace_root):
    path = path_resolver(args.get("path", "."))
    entries = []
    for item in path.iterdir():
        if context.execution_context is not None:
            context.execution_context.check_active()
        if item.name not in IGNORED_PATH_NAMES:
            entries.append(item)
    entries.sort(key=lambda item: (item.is_file(), item.name.lower(), item.name))
    offset, limit = args["offset"], args["limit"]
    if offset > len(entries):
        raise ValueError("directory offset is past the end; restart at offset=0")
    selected = entries[offset:offset + limit]
    next_offset = offset + len(selected) if offset + len(selected) < len(entries) else None
    lines = []
    for entry in selected:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(workspace_root)}")
    relative = path.relative_to(workspace_root).as_posix() or "."
    if next_offset is not None:
        lines.append(f"[More entries: call list_files with path={json.dumps(relative)}, offset={next_offset}, limit={limit}.]")
    return ToolRunnerResult(
        "\n".join(lines) or "(empty)",
        structured={
            "path": relative,
            "returned_count": len(selected),
            "offset": offset,
            "next_offset": next_offset,
            "has_more": next_offset is not None,
        },
    )


def tool_read_file(context, args, *, path_resolver, workspace_root):
    path = path_resolver(args["path"])
    start_line = int(args.get("start_line", 1))
    requested_end_line = int(args.get("end_line", 200))
    digest = hashlib.sha256()
    rendered = bytearray()
    total_lines = 0
    number = 1
    line_start = True
    truncated = False
    actual_end_line = None
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.readline(64 * 1024), b""):
            if context.execution_context is not None:
                context.execution_context.check_active()
            digest.update(chunk)
            total_lines = number
            if start_line <= number <= requested_end_line:
                prefix = f"{number:>4}: ".encode() if line_start else b""
                content = prefix + chunk
                available = READ_FILE_MAX_OUTPUT_BYTES - len(rendered)
                rendered.extend(content[:available])
                truncated |= len(content) > available
                if available > 0:
                    actual_end_line = number
            line_start = chunk.endswith(b"\n")
            if line_start:
                number += 1
    body = rendered.decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")
    body = body.encode("utf-8")[:READ_FILE_MAX_OUTPUT_BYTES].decode(
        "utf-8", errors="ignore"
    )
    if truncated:
        body += "\n[read output truncated; narrow the line range or search for specific content]"
    revision = "sha256:" + digest.hexdigest()
    relative = path.relative_to(workspace_root).as_posix()
    return ToolRunnerResult(
        body,
        structured={
            "path": relative,
            "start_line": start_line,
            "end_line": actual_end_line,
            "total_lines": total_lines,
            "has_more": truncated or total_lines > requested_end_line,
            "truncated": truncated,
            "revision": revision,
        },
    )


def tool_read_artifact(context, args, *, artifact_store, redact_text):
    page = artifact_store.read_slice(
        context.run_id,
        args["artifact_id"],
        args["offset"],
        args["max_bytes"],
    )
    def result_for(characters):
        content = page["content"][:characters]
        end = page["offset"] + len(content.encode("utf-8"))
        has_more = end < page["total_bytes"]
        text = content
        if has_more:
            text += f"\n[More output available; call read_artifact with offset={end}.]"
        return ToolRunnerResult(
            redact_text(text),
            structured={
                "artifact_id": str(args["artifact_id"]),
                "offset": page["offset"],
                "end_offset": end,
                "total_bytes": page["total_bytes"],
                "has_more": has_more,
            },
        )

    # Fit the delivered JSON, including escaping, redaction and page metadata.
    # end_offset advances only over source bytes actually included in this page.
    low, high = 0, len(page["content"])
    while low < high:
        middle = (low + high + 1) // 2
        result = result_for(middle)
        payload = ToolOutcome(
            context.tool_call_id or "manual", "read_artifact", "success",
            "completed", "none", result.content, structured=result.structured,
        ).model_payload()
        size = len(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8"))
        if size <= TOOL_OUTPUT_MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    return result_for(low)


def _bounded_rg_search(root, relative_path, pattern, executable, execution):  # noqa: C901 - bounded process lifecycle
    process = subprocess.Popen(
        [
            executable,
            "-n",
            "--with-filename",
            "--smart-case",
            "--",
            pattern,
            relative_path,
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
    if execution is not None:
        deadline = min(deadline, execution.deadline)
    limited = False
    timed_out = False
    cancelled = False
    try:
        while selector.get_map():
            if execution is not None and execution.token.requested:
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=min(0.1, remaining))
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = buffers[key.data]
                available = SEARCH_MAX_OUTPUT_BYTES - sum(
                    len(value) for value in buffers.values()
                )
                if available <= 0:
                    limited = True
                    break
                target.extend(chunk[:available])
                if len(chunk) > available:
                    limited = True
                    break
            if limited:
                break
        if limited or timed_out or cancelled:
            process.kill()
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    stdout_lines = buffers["stdout"].decode("utf-8", errors="replace").splitlines()
    match_limited = len(stdout_lines) > SEARCH_MAX_MATCHES
    lines = stdout_lines[:SEARCH_MAX_MATCHES]
    stderr = buffers["stderr"].decode("utf-8", errors="replace").strip()
    truncated = bool(limited or match_limited)
    failure = None
    if cancelled:
        failure = FailureInfo("operation_interrupted", "search cancelled; await an explicit request to continue", "user_action_required")
    elif timed_out:
        failure = FailureInfo(
            "search_timeout",
            "search timed out",
            "retry_after_change",
        )
    elif truncated:
        lines.append("[search result limit reached]")
    elif process.returncode not in {0, 1}:
        detail = stderr or f"rg exited with {process.returncode}"
        code = (
            "invalid_search_pattern" if "regex" in detail.lower() else "search_failed"
        )
        failure = FailureInfo(code, detail, "retry_after_change")
    content = "\n".join(lines).replace(str(root) + "/", "")
    if not content and failure is None:
        content = stderr or "(no matches)"
    return ToolRunnerResult(
        content,
        structured={
            "engine": "rg",
            "match_count": len(stdout_lines[:SEARCH_MAX_MATCHES]),
            "truncated": truncated,
            "timed_out": timed_out,
        },
        failure=failure,
    )


def tool_search(context, args, *, path_resolver, workspace_root):
    pattern = str(args.get("pattern", "")).strip()
    path = path_resolver(args.get("path", "."))

    executable = shutil.which("rg")
    if executable is None:
        return ToolRunnerResult(
            "",
            failure=FailureInfo("search_unavailable", "ripgrep (rg) is not installed; install it and retry", "user_action_required"),
        )
    relative_path = path.relative_to(workspace_root).as_posix() or "."
    return _bounded_rg_search(workspace_root, relative_path, pattern, executable, context.execution_context)


def tool_write_file(context, args, *, mutation_service, workspace_root):
    _logical, path = context.execution_plan.paths[0]
    content = str(args["content"])
    receipt = mutation_service.write(path, content)
    relative = path.relative_to(workspace_root).as_posix()
    return ToolRunnerResult(
        content=receipt.diff,
        structured={
            "path": relative,
            "before_revision": receipt.before_revision,
            "after_revision": receipt.after_revision,
        },
    )


def tool_edit_file(context, args, *, mutation_service, workspace_root, original, expected_revision, before_commit):
    _logical, path = context.execution_plan.paths[0]
    old_text = str(args.get("old_text", ""))
    receipt = mutation_service.edit(
        path, old_text, str(args["new_text"]), expected_revision, original=original,
        before_commit=before_commit,
    )
    relative = path.relative_to(workspace_root).as_posix()
    return ToolRunnerResult(
        content=receipt.diff or "(no changes)",
        structured={
            "path": relative,
            "before_revision": receipt.before_revision,
            "after_revision": receipt.after_revision,
        },
    )


def tool_run_shell(context, args, *, command_runner, workspace_root):
    command = str(args["command"])
    try:
        before = capture_repository_state(
            workspace_root,
            command_runner=command_runner,
            execution_context=context.execution_context,
        )
    except RepositorySnapshotError as exc:
        return ToolRunnerResult(
            "",
            structured={"command": command, "repository_changes": []},
            failure=FailureInfo(
                "repository_snapshot_unavailable",
                str(exc),
                "retry_after_change",
            ),
        )
    result = command_runner.run(
        shell_argv(command),
        cwd=workspace_root,
        timeout=RUN_COMMAND_TIMEOUT_SECONDS,
        env={},
        execution_context=context.execution_context,
    )
    snapshot_failure = None
    try:
        after = capture_repository_state(
            workspace_root,
            command_runner=command_runner,
            execution_context=context.execution_context,
        )
        changes = repository_state_changes(before, after)
    except RepositorySnapshotError as exc:
        changes = ()
        snapshot_failure = exc
    output = "\n".join(
        part
        for part in (
            result.stdout.strip(),
            result.stderr.strip(),
        )
        if part
    )
    failure = None
    effect_scope = "none"
    if snapshot_failure is not None:
        effect_scope = "workspace"
        failure = FailureInfo(
            "repository_snapshot_unavailable",
            str(snapshot_failure),
            "user_action_required",
        )
    elif changes:
        effect_scope = "workspace"
        failure = FailureInfo(
            "command_modified_repository",
            "diagnostic command changed repository-visible state without "
            "a trustworthy Run-start preimage: " + ", ".join(changes[:20]),
            "user_action_required",
        )
    elif result.infrastructure_error:
        failure = FailureInfo(
            "command_infrastructure_error",
            result.stderr or "command could not start",
            "retry_after_change",
        )
    elif result.returncode != 0 or result.stop_reason:
        failure = FailureInfo(
            "command_failed",
            result.stop_reason or f"command exited with {result.returncode}",
            "retry_after_change",
        )
    return ToolRunnerResult(
        output,
        structured={
            "command": command,
            "exit_code": result.returncode,
            "stop_reason": result.stop_reason,
            "output_limited": result.output_limited,
            "repository_changes": list(changes[:20]),
        },
        effect_scope=effect_scope,
        failure=failure,
    )


def _workspace_file_plan(context, args, *, path_resolver, workspace_root):
    path = path_resolver(args["path"])
    logical = path.relative_to(workspace_root).as_posix()
    return ToolExecutionPlan("workspace", ((logical, path),))


def build_tool_registry(*, workspace_root, path_resolver, artifact_store, redact_text, mutation_service, command_runner):
    """Each tool declares its schema, policy, validator, runner and effects together."""
    return {
        "list_files": {
            "args_schema": ListFilesArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "List a sorted directory page. Continue with next_offset; restart at zero if the directory changes.",
            "validate": partial(_validate_list_files, path_resolver=path_resolver),
            "run": partial(tool_list_files, path_resolver=path_resolver, workspace_root=workspace_root),
        },
        "read_file": {
            "args_schema": ReadFileArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "Read a UTF-8 file by line range. Line breaks are presented as LF; the revision identifies the original file bytes.",
            "validate": partial(_validate_read_file, path_resolver=path_resolver, workspace_root=workspace_root),
            "run": partial(tool_read_file, path_resolver=path_resolver, workspace_root=workspace_root),
        },
        "read_artifact": {
            "args_schema": ReadArtifactArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "Read up to 8 KiB from a truncated tool-output artifact in the current run.",
            "validate": partial(_validate_read_artifact, artifact_store=artifact_store),
            "run": partial(tool_read_artifact, artifact_store=artifact_store, redact_text=redact_text),
        },
        "search": {
            "args_schema": SearchArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "Search the workspace with ripgrep (rg must be installed).",
            "validate": partial(_validate_search, path_resolver=path_resolver),
            "run": partial(tool_search, path_resolver=path_resolver, workspace_root=workspace_root),
        },
        "run_shell": {
            "args_schema": RunShellArgs,
            "risky": True,
            "workspace_mutating": True,
            "description": "Run one user-approved diagnostic command from the trusted workspace root. Use it for tests, linters, type checks, git status/diff, and reproductions. It is host execution, not a sandbox, and must not modify repository files. Mutating shell commands are not supported by this Runtime.",
            "validate": partial(_validate_run_shell, command_runner=command_runner),
            "run": partial(tool_run_shell, command_runner=command_runner, workspace_root=workspace_root),
        },
        "write_file": {
            "args_schema": WriteFileArgs,
            "risky": True,
            "workspace_mutating": True,
            "state_mutating": True,
            "description": "Create a new UTF-8 text file. The target must not already exist; read and use edit_file for every change to an existing file.",
            "validate": partial(_validate_write_file, mutation_service=mutation_service, workspace_root=workspace_root),
            "run": partial(tool_write_file, mutation_service=mutation_service, workspace_root=workspace_root),
            "plan": partial(_workspace_file_plan, path_resolver=path_resolver, workspace_root=workspace_root),
        },
        "edit_file": {
            "args_schema": EditFileArgs,
            "risky": True,
            "workspace_mutating": True,
            "state_mutating": True,
            "description": "Replace one exact, unique text block in a file, treating LF and CRLF as the same line break. New lines use the local line ending; bytes outside the replaced block are preserved. Keep old_text as small as possible while still unique; do not include large unchanged regions. old_text must contain only actual file content: exclude read_file's line-number prefixes. Read the file first. Runtime internally checks the observed version and rejects external changes; reread after a conflict.",
            "validate": partial(_validate_edit_file, mutation_service=mutation_service),
            "run": partial(tool_edit_file, mutation_service=mutation_service, workspace_root=workspace_root),
            "plan": partial(_workspace_file_plan, path_resolver=path_resolver, workspace_root=workspace_root),
        },
    }
