"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import hashlib
import os
import selectors
import shutil
import subprocess
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .command_runner import shell_argv
from .contracts import (
    TOOL_ARTIFACT_ID_PATTERN,
    FailureInfo,
    ToolFailureError,
    ToolRunnerResult,
)
from .verification import (
    RepositorySnapshotError,
    capture_repository_state,
    repository_state_changes,
)
from .working_state import normalize_working_update
from .workspace import IGNORED_PATH_NAMES

READ_FILE_MAX_OUTPUT_BYTES = 512 * 1024
READ_FILE_MAX_LINES = 2000
SEARCH_MAX_MATCHES = 200
SEARCH_MAX_OUTPUT_BYTES = 512 * 1024
SEARCH_TIMEOUT_SECONDS = 10.0
RUN_COMMAND_TIMEOUT_SECONDS = 120


def history_projection(*, arg_fields, result_fields):
    """Build one tool-owned compact History projection."""

    arg_fields = tuple(arg_fields)
    result_fields = tuple(result_fields)

    def project(args, outcome):
        projected = {
            "args": {
                field: args[field]
                for field in arg_fields
                if field in args
            },
            "outcome": {
                "status": outcome.status,
                "execution_state": outcome.execution_state,
                "side_effect_state": outcome.side_effect_state,
            },
        }
        structured = {
            field: outcome.structured[field]
            for field in result_fields
            if field in outcome.structured
        }
        if structured:
            projected["outcome"]["structured"] = structured
        if outcome.failure is not None:
            projected["outcome"]["failure"] = outcome.failure.to_dict()
        if outcome.affected_paths:
            projected["outcome"]["affected_paths"] = list(outcome.affected_paths)
        if outcome.effect_scope != "none":
            projected["outcome"]["effect_scope"] = outcome.effect_scope
        if outcome.artifact_id:
            projected["outcome"]["artifact_id"] = outcome.artifact_id
        return projected

    return project


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListFilesArgs(ToolArgs):
    path: str = "."


class ReadFileArgs(ToolArgs):
    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)


class ReadArtifactArgs(ToolArgs):
    artifact_id: str = Field(pattern=TOOL_ARTIFACT_ID_PATTERN)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=8192, ge=4, le=8192)


class SearchArgs(ToolArgs):
    pattern: str = Field(min_length=1)
    path: str = "."


class RunCommandArgs(ToolArgs):
    command: str = Field(min_length=1)


class WriteFileArgs(ToolArgs):
    path: str = Field(min_length=1)
    content: str


class EditFileArgs(ToolArgs):
    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    expected_revision: str = Field(
        pattern=r"^sha256:[a-f0-9]{64}$",
        description="Exact sha256 revision returned by read_file",
    )


class SubmitFinalArgs(ToolArgs):
    answer: str = Field(min_length=1)


class UpdateWorkingStateArgs(ToolArgs):
    add_constraints: tuple[str, ...] = Field(default=(), max_length=24)
    remove_constraints: tuple[str, ...] = Field(default=(), max_length=24)
    add_decisions: tuple[str, ...] = Field(default=(), max_length=24)
    remove_decisions: tuple[str, ...] = Field(default=(), max_length=24)
    add_next_steps: tuple[str, ...] = Field(default=(), max_length=24)
    remove_next_steps: tuple[str, ...] = Field(default=(), max_length=24)


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


def _validate_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.exists():
        raise ToolFailureError(
            "missing_path", f"path does not exist: {args.get('path', '.')}"
        )
    if not path.is_dir():
        raise ToolFailureError("invalid_path_type", "path is not a directory")
    return args


def _validate_read_file(context, args):
    path = context.path(args["path"])
    if not path.exists():
        raise ToolFailureError("missing_path", f"path does not exist: {args['path']}")
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


def _validate_read_artifact(context, args):
    if context.artifact_store is None or not context.run_id:
        raise ValueError("artifact store is unavailable")
    return args


def _validate_search(context, args):
    if not str(args.get("pattern", "")).strip():
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))
    if not path.exists():
        raise ToolFailureError(
            "missing_path", f"path does not exist: {args.get('path', '.')}"
        )
    return args


def _require_mutation_service(context):
    if context.mutation_service is None:
        raise ValueError("workspace mutation service is unavailable")


def _validate_write_file(context, args):
    path = context.path(args["path"])
    if path.exists():
        if path.is_dir():
            raise ToolFailureError("invalid_path_type", "path is a directory")
        raise ToolFailureError(
            "existing_file_requires_edit",
            "write_file only creates new files; read the current file and use edit_file",
            structured={
                "path": path.relative_to(context.workspace_root).as_posix(),
                "recommended_next_tool": "read_file",
            },
        )
    _require_mutation_service(context)
    return args


def _validate_edit_file(context, args):
    # Edit admission is intentionally strict so the later mutation is
    # deterministic and revision-bound.
    path = context.path(args["path"])
    if not path.exists():
        raise ToolFailureError("missing_path", f"path does not exist: {args['path']}")
    if not path.is_file():
        raise ToolFailureError("invalid_path_type", "path is not a file")
    _require_mutation_service(context)
    return args


def _validate_working_state(context, args):
    state = context.working_state
    if state is None or not context.tool_call_id:
        raise ValueError("working state updates require an active Run tool call")
    normalized = normalize_working_update(args)
    state.updated(normalized)
    return normalized


def _validate_run_command(context, args):
    command = str(args["command"]).strip()
    if not command:
        raise ValueError("run_command requires a non-blank command")
    if context.command_runner is None:
        raise RuntimeError("run_command requires a CommandRunner")
    return {"command": command}


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    entries = [
        item
        for item in sorted(
            path.iterdir(), key=lambda item: (item.is_file(), item.name.lower())
        )
        if item.name not in IGNORED_PATH_NAMES
    ]
    selected = entries[:200]
    lines = []
    for entry in selected:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.workspace_root)}")
    relative = path.relative_to(context.workspace_root).as_posix() or "."
    return ToolRunnerResult(
        "\n".join(lines) or "(empty)",
        structured={
            "path": relative,
            "returned_count": len(selected),
            "has_more": len(entries) > len(selected),
        },
    )


def tool_read_file(context, args):
    path = context.path(args["path"])
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
    relative = path.relative_to(context.workspace_root).as_posix()
    return ToolRunnerResult(
        f"# {relative}\nrevision: {revision}\n{body}",
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


def tool_read_artifact(context, args):
    page = context.artifact_store.read_slice(
        context.run_id,
        args["artifact_id"],
        args["offset"],
        args["max_bytes"],
    )
    header = (
        f"# artifact {args['artifact_id']}\n"
        f"bytes {page['offset']}-{page['end_offset']} of {page['total_bytes']}\n"
    )
    continuation = ""
    if page["end_offset"] < page["total_bytes"]:
        continuation = (
            "\n[More output available; call read_artifact with "
            f"offset={page['end_offset']}.]"
        )
    return ToolRunnerResult(
        header + page["content"] + continuation,
        structured={
            "artifact_id": str(args["artifact_id"]),
            "offset": page["offset"],
            "end_offset": page["end_offset"],
            "total_bytes": page["total_bytes"],
            "has_more": page["end_offset"] < page["total_bytes"],
        },
    )


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
        lines.append("[search cancelled]")
        failure = FailureInfo("operation_interrupted", "search cancelled", "retry_after_wait")
    elif timed_out:
        lines.append("[search timed out]")
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
    if not content:
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


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    path = context.path(args.get("path", "."))

    executable = shutil.which("rg")
    if executable is None:
        return ToolRunnerResult(
            "Search requires ripgrep (rg); install it and retry.",
            failure=FailureInfo("search_unavailable", "ripgrep is not installed", "user_action_required"),
        )
    relative_path = path.relative_to(context.workspace_root).as_posix() or "."
    return _bounded_rg_search(context.workspace_root, relative_path, pattern, executable, context.execution_context)


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    receipt = context.mutation_service.write(path, content)
    relative = path.relative_to(context.workspace_root).as_posix()
    changed = receipt.changed
    return ToolRunnerResult(
        content=(
            f"wrote {relative} ({len(content)} chars)\n"
            f"before_revision: {receipt.before_revision}\n"
            f"after_revision: {receipt.after_revision}\n"
            f"diff:\n{receipt.diff}"
        ),
        structured={
            "path": relative,
            "changed": changed,
            "before_revision": receipt.before_revision,
            "after_revision": receipt.after_revision,
            "diff_bytes": len(receipt.diff.encode("utf-8")),
            "path_transitions": [
                {
                    "path": relative,
                    "before_state": receipt.before_revision,
                    "after_state": receipt.after_revision,
                }
            ],
        },
        affected_paths=(relative,) if changed else (),
        effect_scope="workspace" if changed else "none",
    )


def tool_edit_file(context, args):
    path = context.path(args["path"])
    old_text = str(args.get("old_text", ""))
    receipt = context.mutation_service.edit(
        path, old_text, str(args["new_text"]), args["expected_revision"]
    )
    relative = path.relative_to(context.workspace_root).as_posix()
    changed = receipt.changed
    return ToolRunnerResult(
        content=(
            f"edited {relative}\n"
            f"before_revision: {receipt.before_revision}\n"
            f"after_revision: {receipt.after_revision}\n"
            f"diff:\n{receipt.diff or '(no changes)'}"
        ),
        structured={
            "path": relative,
            "changed": changed,
            "replacement_count": 1,
            "before_revision": receipt.before_revision,
            "after_revision": receipt.after_revision,
            "diff_bytes": len(receipt.diff.encode("utf-8")),
            "path_transitions": [
                {
                    "path": relative,
                    "before_state": receipt.before_revision,
                    "after_state": receipt.after_revision,
                }
            ],
        },
        affected_paths=(relative,) if changed else (),
        effect_scope="workspace" if changed else "none",
    )


def tool_update_working_state(_context, _args):
    return ToolRunnerResult("working state update accepted")


def tool_run_command(context, args):
    command = str(args["command"])
    try:
        before = capture_repository_state(context.workspace_root)
    except RepositorySnapshotError as exc:
        return ToolRunnerResult(
            f"command not started: {exc}",
            structured={"command": command, "repository_changes": []},
            failure=FailureInfo(
                "repository_snapshot_unavailable",
                str(exc),
                "retry_after_change",
            ),
        )
    result = context.command_runner.run(
        shell_argv(command),
        cwd=context.workspace_root,
        timeout=RUN_COMMAND_TIMEOUT_SECONDS,
        env={},
        execution_context=context.execution_context,
    )
    snapshot_failure = None
    try:
        after = capture_repository_state(context.workspace_root)
        changes = repository_state_changes(before, after)
    except RepositorySnapshotError as exc:
        changes = ()
        snapshot_failure = exc
    output = "\n".join(
        part
        for part in (
            f"command: {command}",
            f"exit_code: {result.returncode}",
            result.stdout.strip(),
            result.stderr.strip(),
            f"stop_reason: {result.stop_reason}" if result.stop_reason else "",
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


def _workspace_file_effects(context, args):
    path = context.path(args["path"])
    logical = path.relative_to(context.workspace_root).as_posix()
    return "workspace", ((logical, path),)


def build_tool_registry():
    """Each tool declares its schema, policy, validator, runner and effects together."""
    return {
        "list_files": {
            "args_schema": ListFilesArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "List files in the workspace.",
            "validate": _validate_list_files,
            "run": tool_list_files,
            "history_projection": history_projection(
                arg_fields=("path",),
                result_fields=("path", "returned_count", "has_more"),
            ),
        },
        "read_file": {
            "args_schema": ReadFileArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "Read a UTF-8 file by line range.",
            "validate": _validate_read_file,
            "run": tool_read_file,
            "history_projection": history_projection(
                arg_fields=("path", "start_line", "end_line"),
                result_fields=(
                    "path",
                    "start_line",
                    "end_line",
                    "total_lines",
                    "has_more",
                    "truncated",
                    "revision",
                ),
            ),
        },
        "read_artifact": {
            "args_schema": ReadArtifactArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "Read up to 8 KiB from a truncated tool-output artifact in the current run.",
            "validate": _validate_read_artifact,
            "run": tool_read_artifact,
            "history_projection": history_projection(
                arg_fields=("artifact_id", "offset", "max_bytes"),
                result_fields=(
                    "artifact_id",
                    "offset",
                    "end_offset",
                    "total_bytes",
                    "has_more",
                ),
            ),
        },
        "search": {
            "args_schema": SearchArgs,
            "risky": False,
            "manual_observation": True,
            "concurrency": "parallel",
            "description": "Search the workspace with ripgrep (rg must be installed).",
            "validate": _validate_search,
            "run": tool_search,
            "history_projection": history_projection(
                arg_fields=("pattern", "path"),
                result_fields=("engine", "match_count", "truncated", "timed_out"),
            ),
        },
        "run_command": {
            "args_schema": RunCommandArgs,
            "risky": True,
            "workspace_mutating": True,
            "description": "Run one user-approved diagnostic command from the trusted workspace root. Use it for tests, linters, type checks, git status/diff, and reproductions. It is host execution, not a sandbox, and must not modify repository files. Mutating shell commands are not supported by this Runtime.",
            "validate": _validate_run_command,
            "run": tool_run_command,
            "history_projection": history_projection(
                arg_fields=("command",),
                result_fields=("command", "repository_changes"),
            ),
        },
        "write_file": {
            "args_schema": WriteFileArgs,
            "risky": True,
            "workspace_mutating": True,
            "state_mutating": True,
            "description": "Create a new UTF-8 text file. The target must not already exist; read and use edit_file for every change to an existing file.",
            "validate": _validate_write_file,
            "run": tool_write_file,
            "potential_effects": _workspace_file_effects,
            "history_projection": history_projection(
                arg_fields=("path",),
                result_fields=(
                    "path",
                    "changed",
                    "before_revision",
                    "after_revision",
                    "diff_bytes",
                    "path_transitions",
                ),
            ),
        },
        "edit_file": {
            "args_schema": EditFileArgs,
            "risky": True,
            "workspace_mutating": True,
            "state_mutating": True,
            "description": "Replace one exact, unique text block in a file. Keep old_text as small as possible while still unique; do not include large unchanged regions. old_text must contain only actual file content: exclude read_file's file/revision headers and line-number prefixes.",
            "validate": _validate_edit_file,
            "run": tool_edit_file,
            "potential_effects": _workspace_file_effects,
            "history_projection": history_projection(
                arg_fields=("path", "expected_revision"),
                result_fields=(
                    "path",
                    "changed",
                    "replacement_count",
                    "before_revision",
                    "after_revision",
                    "diff_bytes",
                    "path_transitions",
                ),
            ),
        },
        "update_working_state": {
            "args_schema": UpdateWorkingStateArgs,
            "risky": False,
            "description": "Incrementally update the current Run's constraints, evidence-backed decisions, and next steps. The Runtime owns the immutable goal. Do not store current file contents, transient command output, guesses, or cross-task project knowledge here.",
            "validate": _validate_working_state,
            "run": tool_update_working_state,
            "history_projection": history_projection(
                arg_fields=(
                    "add_constraints",
                    "remove_constraints",
                    "add_decisions",
                    "remove_decisions",
                    "add_next_steps",
                    "remove_next_steps",
                ),
                result_fields=(),
            ),
        },
    }
