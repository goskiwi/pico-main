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


class SearchArgs(ToolArgs):
    pattern: str = Field(min_length=1)
    path: str = "."


class QueryRepoMapArgs(ToolArgs):
    query: str = Field(min_length=1)
    budget_tokens: int = Field(default=1200, ge=64, le=4000)
    max_results: int = Field(default=24, ge=1, le=60)


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
    "search": {
        "args_schema": SearchArgs,
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "query_repo_map": {
        "args_schema": QueryRepoMapArgs,
        "risky": False,
        "description": "Rank Python symbols and static relations for a repository question.",
    },
    "run_shell": {
        "args_schema": RunShellArgs,
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "args_schema": WriteFileArgs,
        "risky": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "args_schema": PatchFileArgs,
        "risky": True,
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

def legal_tool_names():
    return set(BASE_TOOL_SPECS)

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
    schema["additionalProperties"] = False
    # Strict Responses schemas require every declared property. Runtime
    # defaults still apply to direct/manual calls before runner execution.
    schema["required"] = list(schema.get("properties", {}))
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


def validate_tool(context, name, args):
    if name not in BASE_TOOL_SPECS:
        raise ValueError(f"unknown tool: {name}")
    args = BASE_TOOL_SPECS[name]["args_schema"].model_validate(args or {}).model_dump()

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return args

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return args

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        context.path(args.get("path", "."))
        return args

    if name == "query_repo_map":
        if context.repo_map is None:
            raise ValueError("repository map is unavailable")
        return args

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return args

    if name == "write_file":
        path = context.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        if context.mutation_service is None:
            raise ValueError("workspace mutation service is unavailable")
        return args

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        if context.mutation_service is None:
            raise ValueError("workspace mutation service is unavailable")
        return args

    if name in {"memory_store", "memory_forget"}:
        if context.project_memory is None:
            raise ValueError("project memory is unavailable")
        return args

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
    return "\n".join(lines) or "(empty)"


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
    return f"# {path.relative_to(context.root)}\nrevision: {file_revision(path)}\n{body}"


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=context.root,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

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
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_query_repo_map(context, args):
    result = context.repo_map.render(
        args["query"],
        budget_tokens=args.get("budget_tokens", 1200),
        max_results=args.get("max_results", 24),
    )
    return result.text


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
    return textwrap.dedent(
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


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    before, after = context.mutation_service.write(path, content, args["expected_revision"])
    return (
        f"wrote {path.relative_to(context.root)} ({len(content)} chars)\n"
        f"before_revision: {before}\nafter_revision: {after}"
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
    return f"patched {path.relative_to(context.root)}\nbefore_revision: {before}\nafter_revision: {after}"


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
        origin="explicit",
        source_session_id=context.session_id,
        source_run_id=context.run_id(),
        source_entry_ids=context.source_entry_ids(),
        source_tool_call_id=context.tool_call_id(),
        expires_at=args.get("expires_at", ""),
    )
    if action == "unchanged":
        return (
            f"unchanged project memory {card.filename}; the requested content is already "
            "committed. Do not repeat memory_store; submit the final answer if the task is complete."
        )
    return (
        f"{action} project memory {card.filename}; commit complete. "
        "Do not repeat memory_store for the same fact."
    )


def tool_memory_forget(context, args):
    card = context.project_memory.forget(args["filename"])
    if card is None:
        raise ValueError("memory file does not exist")
    return f"forgot project memory {card.filename}"


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "query_repo_map": tool_query_repo_map,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "memory_store": tool_memory_store,
    "memory_forget": tool_memory_forget,
}
