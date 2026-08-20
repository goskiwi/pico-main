"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import shutil
import subprocess
import textwrap
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ToolExecution
from .mutations import file_revision
from .sandbox import SandboxProfile, parse_command_invocation
from .workspace import IGNORED_PATH_NAMES


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


class MemoryStoreArgs(ToolArgs):
    action: Literal["create", "update"]
    filename: str = Field(pattern=r"^(?:user|feedback|project|reference)_[a-z0-9][a-z0-9_-]{0,55}\.md$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    type: Literal["user", "feedback", "project", "reference"]
    content: str = Field(min_length=1, max_length=1000)
    why: str = Field(default="", max_length=500)
    how_to_apply: str = Field(default="", max_length=500)
    expires_at: str = ""


class MemoryForgetArgs(ToolArgs):
    filename: str = Field(pattern=r"^(?:user|feedback|project|reference)_[a-z0-9][a-z0-9_-]{0,55}\.md$")


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
    "memory_store": {
        "args_schema": MemoryStoreArgs,
        "risky": True,
        "description": (
            "Create or update one explicit Markdown project-memory card. Runtime adds trusted "
            "provenance; do not invent source dates, entry IDs, or provenance claims in content."
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
    if not context.path(args["path"]).is_file():
        raise ValueError("path is not a file")
    if int(args.get("end", 200)) < int(args.get("start", 1)):
        raise ValueError("invalid line range")
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


_TOOL_VALIDATORS = {
    "list_files": _validate_list_files,
    "read_file": _validate_read_file,
    "read_artifact": _validate_read_artifact,
    "search": _validate_search,
    "run_shell": _validate_run_shell,
    "write_file": _validate_write_file,
    "patch_file": _validate_patch_file,
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
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return ToolExecution("\n".join(lines) or "(empty)")


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return ToolExecution(
        f"# {path.relative_to(context.root)}\nrevision: {file_revision(path)}\n{body}"
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
    return ToolExecution(header + page["content"] + continuation)


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        relative_path = path.relative_to(context.root).as_posix() or "."
        result = subprocess.run(
            [
                "rg", "-n", "--with-filename", "--smart-case", "--max-count", "200",
                "--", pattern, relative_path,
            ],
            cwd=context.root,
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout.strip() or result.stderr.strip()
        if output:
            output = output.replace(str(context.root) + "/", "")
        return ToolExecution(output or "(no matches)")

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(context.root).parts)
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(context.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return ToolExecution("\n".join(matches))
    return ToolExecution("\n".join(matches) or "(no matches)")


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    if context.sandbox is None:
        raise RuntimeError("Docker sandbox is unavailable")
    argv, command_env = parse_command_invocation(command)
    result = context.sandbox.run(
        argv,
        cwd=context.root,
        timeout=timeout,
        env={**context.shell_env(), **command_env},
        execution_context=context.execution_context(),
        profile=SandboxProfile.INSPECT,
    )
    return ToolExecution(
        textwrap.dedent(
            f"""\
            exit_code: {result.returncode if result.returncode is not None else -1}
            sandbox: docker (profile=inspect, network=none, read_only_rootfs=true, read_only_workspace=true)
            stop_reason: {result.stop_reason or "none"}
            cleanup_state: {result.cleanup_state}
            output_truncated: {result.output_truncated}
            stdout:
            {result.stdout.strip() or "(empty)"}
            stderr:
            {result.stderr.strip() or "(empty)"}
            """
        ).strip()
    )


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    before, after = context.mutation_service.write(path, content, args["expected_revision"])
    relative = path.relative_to(context.root).as_posix()
    changed = before != after
    return ToolExecution(
        content=(
            f"wrote {relative} ({len(content)} chars)\n"
            f"before_revision: {before}\nafter_revision: {after}"
        ),
        affected_paths=(relative,) if changed else (),
        diff_summary=(f"{'created' if before == 'absent' else 'modified'}:{relative}",)
        if changed
        else (),
        effect_scope="workspace" if changed else "none",
    )


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    before, after = context.mutation_service.patch(
        path, old_text, str(args["new_text"]), args["expected_revision"]
    )
    relative = path.relative_to(context.root).as_posix()
    changed = before != after
    return ToolExecution(
        content=f"patched {relative}\nbefore_revision: {before}\nafter_revision: {after}",
        affected_paths=(relative,) if changed else (),
        diff_summary=(f"modified:{relative}",) if changed else (),
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
            logical.append(path.relative_to(context.root).as_posix())
        except ValueError:
            logical.append(path.as_posix())
    return tuple(logical)


def tool_memory_store(context, args):
    card, action = context.project_memory.store(
        action=args["action"],
        filename=args["filename"],
        name=args["name"],
        description=args["description"],
        memory_type=args["type"],
        content=args["content"],
        why=args.get("why", ""),
        how_to_apply=args.get("how_to_apply", ""),
        source_session_id=context.session_id,
        source_run_id=context.run_id(),
        source_entry_ids=context.source_entry_ids(),
        source_tool_call_id=context.tool_call_id(),
        expires_at=args.get("expires_at", ""),
    )
    if action == "unchanged":
        return ToolExecution(
            f"{action} project memory {card.filename}; no data changed. "
            "Do not repeat memory_store; submit the final answer if the task is complete."
        )
    paths = _memory_effect_paths(context, card.filename)
    return ToolExecution(
        content=(
            f"{action} project memory {card.filename}; commit complete. "
            "Do not repeat memory_store for the same fact."
        ),
        affected_paths=paths,
        diff_summary=tuple(f"modified:{path}" for path in paths),
        effect_scope="project_memory",
    )


def tool_memory_forget(context, args):
    card = context.project_memory.forget(args["filename"])
    if card is None:
        raise ValueError("memory file does not exist")
    paths = _memory_effect_paths(context, card.filename)
    return ToolExecution(
        content=f"forgot project memory {card.filename}",
        affected_paths=paths,
        diff_summary=(f"deleted:{paths[0]}", f"modified:{paths[1]}"),
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
    "memory_store": tool_memory_store,
    "memory_forget": tool_memory_forget,
}
