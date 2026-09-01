"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import hashlib
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
from collections import deque
from functools import partial
from pathlib import Path
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

READ_FILE_MAX_BYTES = 8 * 1024 * 1024
READ_FILE_MAX_LINES = 2000
SEARCH_MAX_FILES = 5000
SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
SEARCH_MAX_MATCHES = 200
SEARCH_MAX_OUTPUT_BYTES = 512 * 1024
SEARCH_TIMEOUT_SECONDS = 10.0
RUN_COMMAND_TIMEOUT_SECONDS = 120


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
    max_bytes: int = Field(default=8192, ge=1, le=8192)


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


BASE_TOOL_SPECS = {
    "list_files": {
        "args_schema": ListFilesArgs,
        "risky": False,
        "manual_observation": True,
        "batchable_observation": True,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "args_schema": ReadFileArgs,
        "risky": False,
        "manual_observation": True,
        "batchable_observation": True,
        "description": "Read a UTF-8 file by line range.",
    },
    "read_artifact": {
        "args_schema": ReadArtifactArgs,
        "risky": False,
        "manual_observation": True,
        "batchable_observation": True,
        "description": (
            "Read up to 8 KiB from a truncated tool-output artifact in the current run."
        ),
    },
    "search": {
        "args_schema": SearchArgs,
        "risky": False,
        "manual_observation": True,
        "batchable_observation": True,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_command": {
        "args_schema": RunCommandArgs,
        "risky": True,
        "workspace_mutating": True,
        "description": (
            "Run one user-approved diagnostic command from the trusted workspace root. "
            "Use it for tests, linters, type checks, git status/diff, and reproductions. "
            "It is host execution, not a sandbox, and must not modify repository files. "
            "Mutating shell commands are not supported by this Runtime."
        ),
    },
    "write_file": {
        "args_schema": WriteFileArgs,
        "risky": True,
        "workspace_mutating": True,
        "state_mutating": True,
        "description": (
            "Create a new UTF-8 text file. The target must not already exist; "
            "read and use edit_file for every change to an existing file."
        ),
    },
    "edit_file": {
        "args_schema": EditFileArgs,
        "risky": True,
        "workspace_mutating": True,
        "state_mutating": True,
        "description": (
            "Replace one exact, unique text block in a file. Keep old_text as small as "
            "possible while still unique; do not include large unchanged regions. old_text "
            "must contain only actual file content: exclude read_file's file/revision headers "
            "and line-number prefixes."
        ),
    },
    "update_working_state": {
        "args_schema": UpdateWorkingStateArgs,
        "risky": False,
        "description": (
            "Incrementally update the current Run's constraints, evidence-backed decisions, "
            "and next steps. The Runtime owns the immutable goal. Do not store current file "
            "contents, transient command output, guesses, or cross-task project knowledge here."
        ),
    },
}


def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {}
    for name, spec in BASE_TOOL_SPECS.items():
        tool = {
            **spec,
            "validate": partial(_TOOL_VALIDATORS[name], context),
            "run": partial(_TOOL_RUNNERS[name], context),
        }
        planner = _TOOL_EFFECT_PLANNERS.get(name)
        if planner is not None:
            tool["potential_effects"] = partial(planner, context)
        tools[name] = tool
    return tools


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
        raise ToolFailureError("missing_path", f"path does not exist: {args.get('path', '.')}")
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
        int(args.get("end_line", 200))
        - int(args.get("start_line", 1))
        + 1
        > READ_FILE_MAX_LINES
    ):
        raise ValueError(f"read_file returns at most {READ_FILE_MAX_LINES} lines")
    if path.stat().st_size > READ_FILE_MAX_BYTES:
        raise ValueError(
            f"read_file target exceeds {READ_FILE_MAX_BYTES} bytes; use search or a narrower artifact"
        )
    return args


def _validate_read_artifact(context, args):
    if context.artifact_store is None or not context.run_id():
        raise ValueError("artifact store is unavailable")
    return args


def _validate_search(context, args):
    if not str(args.get("pattern", "")).strip():
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))
    if not path.exists():
        raise ToolFailureError("missing_path", f"path does not exist: {args.get('path', '.')}")
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
    state = context.working_state()
    if state is None or not context.tool_call_id():
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


_TOOL_VALIDATORS = {
    "list_files": _validate_list_files,
    "read_file": _validate_read_file,
    "read_artifact": _validate_read_artifact,
    "search": _validate_search,
    "run_command": _validate_run_command,
    "write_file": _validate_write_file,
    "edit_file": _validate_edit_file,
    "update_working_state": _validate_working_state,
}


def validate_tool(context, name, args):
    spec = BASE_TOOL_SPECS.get(name)
    if spec is None:
        raise ValueError(f"unknown tool: {name}")
    validated = spec["args_schema"].model_validate(args or {}).model_dump()
    return _TOOL_VALIDATORS[name](context, validated)


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
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
    rendered = []
    total_lines = 0
    with path.open("rb") as handle:
        for number, raw_line in enumerate(handle, start=1):
            digest.update(raw_line)
            total_lines = number
            if start_line <= number <= requested_end_line:
                line = raw_line.decode("utf-8", errors="replace")
                line = line.removesuffix("\n").removesuffix("\r")
                rendered.append(f"{number:>4}: {line}")
    body = "\n".join(rendered)
    revision = "sha256:" + digest.hexdigest()
    actual_end_line = (
        min(requested_end_line, total_lines)
        if total_lines >= start_line
        else None
    )
    relative = path.relative_to(context.workspace_root).as_posix()
    return ToolRunnerResult(
        f"# {relative}\nrevision: {revision}\n{body}",
        structured={
            "path": relative,
            "start_line": start_line,
            "end_line": actual_end_line,
            "total_lines": total_lines,
            "has_more": total_lines > requested_end_line,
            "revision": revision,
        },
    )


def tool_read_artifact(context, args):
    page = context.artifact_store.read_slice(
        context.run_id(),
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


def _bounded_rg_search(root, relative_path, pattern):  # noqa: C901 - bounded process lifecycle
    process = subprocess.Popen(
        [
            "rg",
            "-n",
            "--with-filename",
            "--smart-case",
            "--max-columns",
            "2000",
            "--max-filesize",
            str(SEARCH_MAX_FILE_BYTES),
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
    limited = False
    timed_out = False
    try:
        while selector.get_map():
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
        if limited or timed_out:
            process.kill()
        process.wait(timeout=2)
    finally:
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
    if timed_out:
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
        code = "invalid_search_pattern" if "regex" in detail.lower() else "search_failed"
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


def _fallback_search_files(path, deadline):
    if path.is_file():
        return [path], False
    files = []
    pending = deque([path])
    limited = False
    while pending:
        if time.monotonic() >= deadline or len(files) >= SEARCH_MAX_FILES:
            limited = True
            break
        directory = pending.popleft()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name in IGNORED_PATH_NAMES:
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                continue
            candidate = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(candidate)
            elif (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_size <= SEARCH_MAX_FILE_BYTES
            ):
                files.append(candidate)
                if len(files) >= SEARCH_MAX_FILES:
                    limited = True
                    break
    return files, limited


def _fallback_search(context, path, pattern):
    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
    flags = 0 if any(character.isupper() for character in pattern) else re.IGNORECASE
    try:
        expression = re.compile(pattern, flags)
    except re.error as exc:
        return ToolRunnerResult(
            f"invalid search pattern: {exc}",
            structured={
                "engine": "python_regex",
                "match_count": 0,
                "truncated": False,
                "timed_out": False,
            },
            failure=FailureInfo(
                "invalid_search_pattern",
                str(exc),
                "retry_after_change",
            ),
        )
    files, limited = _fallback_search_files(path, deadline)
    matches = []
    timed_out = False
    for file_path in files:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    if expression.search(line):
                        line = line.removesuffix("\n").removesuffix("\r")
                        matches.append(
                            f"{file_path.relative_to(context.workspace_root)}:{number}:{line}"
                        )
                        if len(matches) >= SEARCH_MAX_MATCHES:
                            limited = True
                            break
        except OSError:
            continue
        if limited or timed_out:
            break
    if timed_out:
        matches.append("[search timed out]")
    elif limited:
        matches.append("[search result limit reached]")
    return ToolRunnerResult(
        "\n".join(matches) or "(no matches)",
        structured={
            "engine": "python_regex",
            "match_count": len(matches) - int(limited or timed_out),
            "truncated": bool(limited),
            "timed_out": timed_out,
        },
        failure=(
            FailureInfo(
                "search_timeout",
                "search timed out",
                "retry_after_change",
            )
            if timed_out
            else None
        ),
    )


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        relative_path = path.relative_to(context.workspace_root).as_posix() or "."
        return _bounded_rg_search(context.workspace_root, relative_path, pattern)
    return _fallback_search(context, path, pattern)


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
        execution_context=context.execution_context(),
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
            "a trustworthy Run-start preimage: "
            + ", ".join(changes[:20]),
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


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "read_artifact": tool_read_artifact,
    "search": tool_search,
    "run_command": tool_run_command,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "update_working_state": tool_update_working_state,
}


def _workspace_file_effects(context, args):
    path = context.path(args["path"])
    logical = path.relative_to(context.workspace_root).as_posix()
    return "workspace", ((logical, path),)


_TOOL_EFFECT_PLANNERS = {
    "write_file": _workspace_file_effects,
    "edit_file": _workspace_file_effects,
}
