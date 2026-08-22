"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import os
import selectors
import shutil
import stat
import subprocess
import textwrap
import time
from collections import deque
from functools import partial
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import FailureInfo, ToolRunnerResult
from .features.memory import normalize_working_update
from .mutations import file_revision
from .project_memory import MEMORY_RECALL_MAX_CARDS
from .sandbox import parse_command_invocation
from .workspace import IGNORED_PATH_NAMES

READ_FILE_MAX_BYTES = 8 * 1024 * 1024
READ_FILE_MAX_LINES = 2000
SEARCH_MAX_FILES = 5000
SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
SEARCH_MAX_MATCHES = 200
SEARCH_MAX_OUTPUT_BYTES = 512 * 1024
SEARCH_TIMEOUT_SECONDS = 10.0
MEMORY_RECALL_MAX_TOKENS = 2400


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListFilesArgs(ToolArgs):
    path: str = "."


class ReadFileArgs(ToolArgs):
    path: str = Field(min_length=1)
    start: int = Field(default=1, ge=1)
    end: int = Field(default=200, ge=1)


class ReadArtifactArgs(ToolArgs):
    artifact_id: str = Field(min_length=1, max_length=240)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=8192, ge=1, le=8192)


class SearchArgs(ToolArgs):
    pattern: str = Field(min_length=1)
    path: str = "."


class RunShellArgs(ToolArgs):
    command: str = Field(min_length=1)
    timeout: int = Field(default=20, ge=1, le=120)


class WriteFileArgs(ToolArgs):
    path: str = Field(min_length=1)
    content: str
    expected_revision: str = Field(
        pattern=r"^(?:absent|sha256:[a-f0-9]{64})$",
        description="Revision returned by read_file, or 'absent' for a new file",
    )


class PatchFileArgs(ToolArgs):
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


class MemoryStoreArgs(ToolArgs):
    action: Literal["create", "update"]
    filename: str = Field(pattern=r"^(?:user|feedback|project|reference)_[a-z0-9][a-z0-9_-]{0,55}\.md$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    memory_type: Literal["user", "feedback", "project", "reference"]
    content: str = Field(min_length=1, max_length=1000)
    why: str = Field(default="", max_length=500)
    how_to_apply: str = Field(default="", max_length=500)
    expires_at: str = ""


class MemoryForgetArgs(ToolArgs):
    filename: str = Field(pattern=r"^(?:user|feedback|project|reference)_[a-z0-9][a-z0-9_-]{0,55}\.md$")


class MemoryRecallArgs(ToolArgs):
    filenames: tuple[str, ...] = Field(
        min_length=1,
        max_length=MEMORY_RECALL_MAX_CARDS,
    )


BASE_TOOL_SPECS = {
    "list_files": {
        "args_schema": ListFilesArgs,
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "args_schema": ReadFileArgs,
        "risky": False,
        "description": "Read a UTF-8 file by line range.",
    },
    "read_artifact": {
        "args_schema": ReadArtifactArgs,
        "risky": False,
        "description": (
            "Read up to 8 KiB from a truncated tool-output artifact in the current run."
        ),
    },
    "search": {
        "args_schema": SearchArgs,
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "args_schema": RunShellArgs,
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "args_schema": WriteFileArgs,
        "risky": True,
        "workspace_mutating": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "args_schema": PatchFileArgs,
        "risky": True,
        "workspace_mutating": True,
        "description": (
            "Replace one exact text block in a file. old_text must contain only actual file "
            "content: exclude read_file's file/revision headers and line-number prefixes."
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
    "memory_recall": {
        "args_schema": MemoryRecallArgs,
        "risky": False,
        "description": (
            "Recall one to five complete Project Memory cards by exact filename from the "
            "visible Catalog. Use only for relevant user preferences, prior feedback, stable "
            "project conventions, or reference procedures. Current workspace facts must be "
            "checked with workspace tools."
        ),
    },
    "memory_store": {
        "args_schema": MemoryStoreArgs,
        "risky": True,
        "description": (
            "Create or update one explicit Markdown project-memory card. Runtime adds source "
            "Run and Tool Call provenance; do not put provenance claims in memory content."
        ),
    },
    "memory_forget": {
        "args_schema": MemoryForgetArgs,
        "risky": True,
        "description": "Delete one explicit Markdown project-memory card.",
    },
}


def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
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
    if not context.path(args.get("path", ".")).is_dir():
        raise ValueError("path is not a directory")
    return args


def _validate_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    if int(args.get("end", 200)) < int(args.get("start", 1)):
        raise ValueError("invalid line range")
    if int(args.get("end", 200)) - int(args.get("start", 1)) + 1 > READ_FILE_MAX_LINES:
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
    context.path(args.get("path", "."))
    return args


def _validate_run_shell(_context, args):
    if not str(args.get("command", "")).strip():
        raise ValueError("command must not be empty")
    return args


def _require_mutation_service(context):
    if context.mutation_service is None:
        raise ValueError("workspace mutation service is unavailable")


def _validate_write_file(context, args):
    path = context.path(args["path"])
    if path.exists() and path.is_dir():
        raise ValueError("path is a directory")
    _require_mutation_service(context)
    return args


def _validate_patch_file(context, args):
    # Patch admission is intentionally strict so the later mutation is
    # deterministic and revision-bound.
    if not context.path(args["path"]).is_file():
        raise ValueError("path is not a file")
    _require_mutation_service(context)
    return args


def _validate_project_memory(context, args):
    if context.project_memory is None:
        raise ValueError("project memory is unavailable")
    return args


def _validate_memory_recall(context, args):
    _validate_project_memory(context, args)
    filenames = tuple(str(item).strip() for item in args["filenames"])
    context.project_memory.recall_cards(filenames)
    return {"filenames": filenames}


def _validate_working_state(context, args):
    state = context.working_state()
    if state is None or not context.tool_call_id():
        raise ValueError("working state updates require an active Run tool call")
    normalized = normalize_working_update(args)
    state.updated(normalized)
    return normalized


_TOOL_VALIDATORS = {
    "list_files": _validate_list_files,
    "read_file": _validate_read_file,
    "read_artifact": _validate_read_artifact,
    "search": _validate_search,
    "run_shell": _validate_run_shell,
    "write_file": _validate_write_file,
    "patch_file": _validate_patch_file,
    "update_working_state": _validate_working_state,
    "memory_recall": _validate_memory_recall,
    "memory_store": _validate_project_memory,
    "memory_forget": _validate_project_memory,
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
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.workspace_root)}")
    return ToolRunnerResult("\n".join(lines) or "(empty)")


def tool_read_file(context, args):
    path = context.path(args["path"])
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = islice(handle, start - 1, end)
        rendered = []
        for number, line in enumerate(lines, start=start):
            line = line.removesuffix("\n").removesuffix("\r")
            rendered.append(f"{number:>4}: {line}")
        body = "\n".join(rendered)
    return ToolRunnerResult(
        f"# {path.relative_to(context.workspace_root)}\nrevision: {file_revision(path)}\n{body}"
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
    return ToolRunnerResult(header + page["content"] + continuation)


def _bounded_rg_search(root, relative_path, pattern):
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
    if timed_out:
        lines.append("[search timed out]")
    elif limited or match_limited:
        lines.append("[search result limit reached]")
    if lines:
        return "\n".join(lines).replace(str(root) + "/", "")
    stderr = buffers["stderr"].decode("utf-8", errors="replace").strip()
    return stderr or "(no matches)"


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
    files, limited = _fallback_search_files(path, deadline)
    matches = []
    needle = pattern.lower()
    for file_path in files:
        if time.monotonic() >= deadline:
            limited = True
            break
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if time.monotonic() >= deadline:
                        limited = True
                        break
                    if needle in line.lower():
                        line = line.removesuffix("\n").removesuffix("\r")
                        matches.append(
                            f"{file_path.relative_to(context.workspace_root)}:{number}:{line}"
                        )
                        if len(matches) >= SEARCH_MAX_MATCHES:
                            limited = True
                            break
        except OSError:
            continue
        if limited:
            break
    if limited:
        matches.append("[search result limit reached]")
    return "\n".join(matches) or "(no matches)"


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        relative_path = path.relative_to(context.workspace_root).as_posix() or "."
        return ToolRunnerResult(
            _bounded_rg_search(context.workspace_root, relative_path, pattern)
        )
    return ToolRunnerResult(_fallback_search(context, path, pattern))


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    timeout = int(args.get("timeout", 20))
    if context.sandbox is None:
        raise RuntimeError("Docker sandbox is unavailable")
    argv, command_env = parse_command_invocation(command)
    result = context.sandbox.run(
        argv,
        cwd=context.workspace_root,
        timeout=timeout,
        env={**context.shell_env(), **command_env},
        execution_context=context.execution_context(),
    )
    if result.cancelled:
        failure = FailureInfo(
            "command_cancelled",
            result.stop_reason or "command cancelled",
            False,
        )
    elif result.timed_out:
        failure = FailureInfo(
            "command_timeout",
            result.stop_reason or "command timed out",
            True,
        )
    elif result.output_limited:
        failure = FailureInfo(
            "command_output_limit",
            result.stop_reason or "command exceeded the output limit",
            False,
        )
    elif result.killed:
        failure = FailureInfo(
            "command_killed",
            result.stop_reason or "command was killed",
            False,
        )
    elif result.returncode is None:
        failure = FailureInfo(
            "command_result_missing",
            result.stop_reason or "command did not report an exit code",
            True,
        )
    elif result.returncode != 0:
        failure = FailureInfo(
            "command_failed",
            f"command exited with {result.returncode}",
            True,
        )
    else:
        failure = None
    return ToolRunnerResult(
        textwrap.dedent(
            f"""\
            exit_code: {result.returncode if result.returncode is not None else -1}
            sandbox: docker (network=none, read_only_rootfs=true, read_only_workspace=true)
            stop_reason: {result.stop_reason or "none"}
            cleanup_state: {result.cleanup_state}
            output_limited: {result.output_limited}
            stdout:
            {result.stdout.strip() or "(empty)"}
            stderr:
            {result.stderr.strip() or "(empty)"}
            """
        ).strip(),
        failure=failure,
    )


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    before, after = context.mutation_service.write(path, content, args["expected_revision"])
    relative = path.relative_to(context.workspace_root).as_posix()
    changed = before != after
    return ToolRunnerResult(
        content=(
            f"wrote {relative} ({len(content)} chars)\n"
            f"before_revision: {before}\nafter_revision: {after}"
        ),
        affected_paths=(relative,) if changed else (),
        effect_scope="workspace" if changed else "none",
    )


def tool_patch_file(context, args):
    path = context.path(args["path"])
    old_text = str(args.get("old_text", ""))
    before, after = context.mutation_service.patch(
        path, old_text, str(args["new_text"]), args["expected_revision"]
    )
    relative = path.relative_to(context.workspace_root).as_posix()
    changed = before != after
    return ToolRunnerResult(
        content=f"patched {relative}\nbefore_revision: {before}\nafter_revision: {after}",
        affected_paths=(relative,) if changed else (),
        effect_scope="workspace" if changed else "none",
    )


def _memory_effect_paths(context, filename):
    paths = (
        context.project_memory.cards_root / filename,
        context.project_memory.index_path,
    )
    logical = []
    for path in paths:
        try:
            logical.append(path.relative_to(context.workspace_root).as_posix())
        except ValueError:
            logical.append(path.as_posix())
    return tuple(logical)


def tool_memory_store(context, args):
    card, action = context.project_memory.store(
        action=args["action"],
        filename=args["filename"],
        name=args["name"],
        description=args["description"],
        memory_type=args["memory_type"],
        content=args["content"],
        why=args.get("why", ""),
        how_to_apply=args.get("how_to_apply", ""),
        source_run_id=context.run_id(),
        source_tool_call_id=context.tool_call_id(),
        expires_at=args.get("expires_at", ""),
    )
    if action == "unchanged":
        return ToolRunnerResult(
            f"{action} project memory {card.filename}; no data changed. "
            "Do not repeat memory_store; submit the final answer if the task is complete."
        )
    paths = _memory_effect_paths(context, card.filename)
    return ToolRunnerResult(
        content=(
            f"{action} project memory {card.filename}; commit complete. "
            "Do not repeat memory_store for the same fact."
        ),
        affected_paths=paths,
        effect_scope="project_memory",
    )


def tool_update_working_state(_context, _args):
    return ToolRunnerResult("working state update accepted")


def tool_memory_recall(context, args):
    cards = context.project_memory.recall_cards(args["filenames"])
    rendered, included = context.project_memory.render_recalled_with_budget(
        cards,
        max_tokens=MEMORY_RECALL_MAX_TOKENS,
        token_counter=context.count_tokens,
    )
    included_names = tuple(card.filename for card in included)
    omitted_names = tuple(card.filename for card in cards if card not in included)
    lines = ["recalled project memory: " + ", ".join(included_names)]
    if omitted_names:
        lines.append(
            "omitted by recall budget: " + ", ".join(omitted_names)
        )
    lines.extend(["", rendered])
    return ToolRunnerResult("\n".join(lines))


def tool_memory_forget(context, args):
    card = context.project_memory.forget(args["filename"])
    if card is None:
        raise ValueError("memory file does not exist")
    paths = _memory_effect_paths(context, card.filename)
    return ToolRunnerResult(
        content=f"forgot project memory {card.filename}",
        affected_paths=paths,
        effect_scope="project_memory",
    )


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "read_artifact": tool_read_artifact,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "update_working_state": tool_update_working_state,
    "memory_recall": tool_memory_recall,
    "memory_store": tool_memory_store,
    "memory_forget": tool_memory_forget,
}
