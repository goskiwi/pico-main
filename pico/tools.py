"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import re
import shlex
import shutil
import subprocess
from functools import partial
from pathlib import Path

from . import security
from .config import (
    ALLOWED_SHELL_COMMANDS,
    PROTECTED_WRITE_FILENAMES,
    PROTECTED_WRITE_PATH_PARTS,
    IGNORED_PATH_NAMES,
    _DANGEROUS_SHELL_PATTERNS_RAW,
)
from .workspace import clip

# 在 import 时编译正则，避免每次调用都重编译
DANGEROUS_SHELL_PATTERNS = tuple(
    (reason, re.compile(pattern)) for reason, pattern in _DANGEROUS_SHELL_PATTERNS_RAW
)

BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {
            "path": {"type": "str", "default": "."},
        },
        "capability": "read",
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {
            "path": {"type": "str", "required": True, "min_length": 1},
            "start": {"type": "int", "default": 1, "min": 1},
            "end": {"type": "int", "default": 200, "min": 1},
        },
        "capability": "read",
        "risky": False,
        "description": "Read a UTF-8 file by line range.",
    },
    "read_tool_output": {
        "schema": {
            "node_id": {"type": "str", "required": True, "min_length": 1},
            "run_id": {"type": "str", "default": ""},
        },
        "capability": "read",
        "risky": False,
        "description": "Read full saved tool output by task graph node id.",
    },
    "search": {
        "schema": {
            "pattern": {"type": "str", "required": True, "min_length": 1},
            "path": {"type": "str", "default": "."},
        },
        "capability": "read",
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "schema": {
            "command": {"type": "str", "required": True, "min_length": 1},
            "timeout": {"type": "int", "default": 20, "min": 1, "max": 120},
        },
        "capability": "execute",
        "risky": True,
        "description": "Run a shell command in the mandatory isolated Docker sandbox.",
    },
    "write_file": {
        "schema": {
            "path": {"type": "str", "required": True, "min_length": 1},
            "content": {"type": "str", "required": True},
        },
        "capability": "write",
        "risky": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "schema": {
            "path": {"type": "str", "required": True, "min_length": 1},
            "old_text": {"type": "str", "required": True, "min_length": 1},
            "new_text": {"type": "str", "required": True},
        },
        "capability": "write",
        "risky": True,
        "description": "Replace one exact text block in a file.",
    },
}

DELEGATE_TOOL_SPEC = {
    "schema": {
        "role": {"type": "str", "required": True, "min_length": 1},
        "task": {"type": "str", "required": True, "min_length": 1},
        "max_steps": {"type": "int", "default": 3, "min": 1, "max": 12},
    },
    "capability": "delegate",
    "risky": False,
    "description": "Ask a bounded read-only child agent with a specific role to investigate.",
}

DELEGATE_MANY_TOOL_SPEC = {
    "schema": {
        "tasks": {"type": "list", "required": True, "min_length": 1, "max_length": 6},
    },
    "capability": "delegate",
    "risky": False,
    "description": "Ask several bounded read-only child agents to investigate separate tasks.",
}

DELEGATE_ROLES = {
    "explore": {
        "allowed_tools": ("list_files", "read_file", "search"),
        "instruction": "Explore the repository for facts relevant to the task. Do not propose edits unless asked to report risks.",
    },
    "review": {
        "allowed_tools": ("list_files", "read_file", "search"),
        "instruction": "Review the relevant code for bugs, regressions, missing tests, and safety issues. Report findings with file references when possible.",
    },
    "verify": {
        "allowed_tools": ("list_files", "read_file", "search"),
        "instruction": "Verify the current state by inspecting files, test output, and evidence. Report what is confirmed and what remains uncertain.",
    },
}

TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "read_tool_output": '<tool>{"name":"read_tool_output","args":{"node_id":"t001_read_file"}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"role":"explore","task":"inspect README.md","max_steps":3}}</tool>',
    "delegate_many": '<tool>{"name":"delegate_many","args":{"tasks":[{"role":"explore","task":"inspect runtime.py","max_steps":3},{"role":"review","task":"review tool safety","max_steps":3}]}}</tool>',
}


def build_tool_registry(agent):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], agent)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if agent.depth < agent.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, agent)}
        tools["delegate_many"] = {**DELEGATE_MANY_TOOL_SPEC, "run": partial(tool_delegate_many, agent)}
    if agent.allowed_tools is not None:
        allowed = set(agent.allowed_tools)
        tools = {name: tool for name, tool in tools.items() if name in allowed}
    return tools


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")


def schema_field_display(spec):
    field_type = str(spec.get("type", "str"))
    required = bool(spec.get("required", False))
    default = spec.get("default")
    if required:
        return field_type
    if default is not None:
        return f"{field_type}={default!r}"
    return field_type


def schema_display(schema):
    return {name: schema_field_display(spec) for name, spec in schema.items()}


def responses_action_tools(tool_registry):
    """Build strict Responses API function definitions for one agent turn."""
    definitions = []
    for name, tool in sorted(dict(tool_registry).items()):
        properties = {
            field_name: _responses_schema_property(name, field_name, field_spec)
            for field_name, field_spec in tool["schema"].items()
        }
        definitions.append(
            {
                "type": "function",
                "name": name,
                "description": str(tool.get("description", "")),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
                "strict": True,
            }
        )
    definitions.append(
        {
            "type": "function",
            "name": "submit_final",
            "description": "Finish the task only after all required workspace work is complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Concise final answer describing completed work and verification.",
                    }
                },
                "required": ["answer"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    )
    return definitions


def _responses_schema_property(tool_name, field_name, spec):
    field_type = str(spec.get("type", "str"))
    if field_type == "str":
        result = {"type": "string"}
        if int(spec.get("min_length", 0) or 0) > 0:
            result["minLength"] = int(spec["min_length"])
    elif field_type == "int":
        result = {"type": "integer"}
        if "min" in spec:
            result["minimum"] = int(spec["min"])
        if "max" in spec:
            result["maximum"] = int(spec["max"])
    elif field_type == "list":
        result = {"type": "array"}
        if tool_name == "delegate_many" and field_name == "tasks":
            result["items"] = {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": sorted(DELEGATE_ROLES)},
                    "task": {"type": "string", "minLength": 1},
                    "max_steps": {"type": "integer", "minimum": 1, "maximum": 12},
                },
                "required": ["role", "task", "max_steps"],
                "additionalProperties": False,
            }
        else:
            result["items"] = {}
        if int(spec.get("min_length", 0) or 0) > 0:
            result["minItems"] = int(spec["min_length"])
        if spec.get("max_length") is not None:
            result["maxItems"] = int(spec["max_length"])
    else:
        raise ValueError(f"unsupported Responses schema type: {field_type}")
    descriptions = {
        ("write_file", "path"): "Workspace-relative path to create or replace.",
        ("write_file", "content"): "Complete file content to write, including imports and final newline when appropriate.",
        ("patch_file", "path"): "Workspace-relative path to edit.",
        ("patch_file", "old_text"): "Exact existing file text without display line numbers; it must occur exactly once.",
        ("patch_file", "new_text"): "Complete replacement for old_text.",
        ("read_file", "path"): "Workspace-relative file path.",
        ("run_shell", "command"): "Shell command to run inside the isolated workspace sandbox.",
    }
    notes = []
    if descriptions.get((tool_name, field_name)):
        notes.append(descriptions[(tool_name, field_name)])
    if "default" in spec:
        notes.append(f"Use {spec['default']!r} when the default behavior is intended.")
    if notes:
        result["description"] = " ".join(notes)
    return result


def validate_schema(schema, args):
    args = args or {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    for name, spec in schema.items():
        required = bool(spec.get("required", False))
        if required and name not in args:
            raise ValueError(f"missing required argument: {name}")
        if name not in args:
            continue
        value = args.get(name)
        expected_type = str(spec.get("type", "str"))
        if expected_type == "str":
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            min_length = int(spec.get("min_length", 0) or 0)
            if min_length and len(value.strip()) < min_length:
                raise ValueError(f"{name} must not be empty")
        elif expected_type == "int":
            try:
                int_value = int(value)
            except Exception as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if "min" in spec and int_value < int(spec["min"]):
                raise ValueError(f"{name} must be >= {spec['min']}")
            if "max" in spec and int_value > int(spec["max"]):
                raise ValueError(f"{name} must be <= {spec['max']}")
        elif expected_type == "list":
            if not isinstance(value, list):
                raise ValueError(f"{name} must be a list")
            min_length = int(spec.get("min_length", 0) or 0)
            if min_length and len(value) < min_length:
                raise ValueError(f"{name} must contain at least {min_length} item(s)")
            max_length = spec.get("max_length")
            if max_length is not None and len(value) > int(max_length):
                raise ValueError(f"{name} must contain at most {max_length} item(s)")
        else:
            raise ValueError(f"unsupported schema type for {name}: {expected_type}")


def detect_dangerous_shell_command(command):
    normalized = " ".join(str(command or "").strip().split())
    for reason, pattern in DANGEROUS_SHELL_PATTERNS:
        if pattern.search(normalized):
            return reason
    return ""


def shell_command_policy(command):
    command = str(command or "").strip()
    if not command:
        return {
            "allowed": False,
            "matched_prefix": "",
            "reason": "empty_command",
        }
    if any(marker in command for marker in (";", "&", "|", ">", "<", "$(", "`", "\n", "\r")):
        return {
            "allowed": False,
            "matched_prefix": "",
            "reason": "shell_composition",
        }
    try:
        parts = shlex.split(command)
    except ValueError:
        return {
            "allowed": False,
            "matched_prefix": "",
            "reason": "parse_error",
        }
    if not parts:
        return {
            "allowed": False,
            "matched_prefix": "",
            "reason": "empty_command",
        }
    for allowed in ALLOWED_SHELL_COMMANDS:
        if tuple(parts[: len(allowed)]) == allowed:
            return {
                "allowed": True,
                "matched_prefix": " ".join(allowed),
                "reason": "allowlisted",
            }
    return {
        "allowed": False,
        "matched_prefix": "",
        "reason": "not_allowlisted",
    }


def protected_write_reason(agent, raw_path):
    path = agent.path(raw_path)
    relative_parts = path.relative_to(agent.root).parts
    for part in relative_parts:
        if part in PROTECTED_WRITE_PATH_PARTS:
            return f"protected workspace path: {part}"
    if path.name in PROTECTED_WRITE_FILENAMES:
        return f"protected secret-like file: {path.name}"
    return ""


def protected_read_reason(agent, raw_path):
    path = agent.path(raw_path)
    if path.name.startswith(".env"):
        return f"protected secret-like file: {path.name}"
    return ""


def validate_tool(agent, name, args):
    args = args or {}
    tool = agent.tools.get(name, {})
    validate_schema(tool.get("schema", {}), args)

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = agent.path(args["path"])
        protected_reason = protected_read_reason(agent, args["path"])
        if protected_reason:
            raise ValueError(f"protected read path blocked: {protected_reason}")
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "read_tool_output":
        node_id = str(args.get("node_id", "")).strip()
        if not node_id:
            raise ValueError("node_id must not be empty")
        run_id = str(args.get("run_id", "")).strip()
        if ".." in node_id or "/" in node_id or "\\" in node_id:
            raise ValueError("invalid node_id")
        if run_id and (".." in run_id or "/" in run_id or "\\" in run_id):
            raise ValueError("invalid run_id")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        raw_path = args.get("path", ".")
        agent.path(raw_path)
        protected_reason = protected_read_reason(agent, raw_path)
        if protected_reason:
            raise ValueError(f"protected read path blocked: {protected_reason}")
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        dangerous_reason = detect_dangerous_shell_command(command)
        if dangerous_reason:
            raise ValueError(f"dangerous shell command blocked: {dangerous_reason}")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = agent.path(args["path"])
        protected_reason = protected_write_reason(agent, args["path"])
        if protected_reason:
            raise ValueError(f"protected write path blocked: {protected_reason}")
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = agent.path(args["path"])
        protected_reason = protected_write_reason(agent, args["path"])
        if protected_reason:
            raise ValueError(f"protected write path blocked: {protected_reason}")
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        validate_delegate_task(args, label="delegate")
        return

    if name == "delegate_many":
        tasks = args.get("tasks", [])
        for index, task_args in enumerate(tasks, start=1):
            if not isinstance(task_args, dict):
                raise ValueError(f"tasks[{index}] must be an object")
            validate_delegate_task(task_args, label=f"tasks[{index}]")
        return


def validate_delegate_task(args, label="delegate"):
    role = str(args.get("role", "")).strip()
    if not role:
        raise ValueError(f"{label}.role must not be empty")
    if role not in DELEGATE_ROLES:
        raise ValueError(f"unsupported delegate role: {role}")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError(f"{label}.task must not be empty")
    max_steps = int(args.get("max_steps", 3))
    if max_steps < 1 or max_steps > 12:
        raise ValueError(f"{label}.max_steps must be in [1, 12]")


def tool_list_files(agent, args):
    path = agent.path(args.get("path", "."))
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(agent.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(agent, args):
    path = agent.path(args["path"])
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(agent.root)}\n{body}"


def tool_read_tool_output(agent, args):
    node_id = str(args.get("node_id", "")).strip()
    run_id = str(args.get("run_id", "")).strip()
    if not run_id:
        task_state = getattr(agent, "current_task_state", None)
        run_id = str(getattr(task_state, "run_id", "") or "")
    if not run_id:
        raise ValueError("run_id is required when no current run is active")

    run_dir = agent.run_store.run_dir(run_id).resolve()
    graph_path = agent.run_store.task_graph_path(run_id).resolve()
    if not graph_path.is_file():
        raise ValueError(f"task graph not found for run_id: {run_id}")
    graph_text = graph_path.read_text(encoding="utf-8", errors="replace")
    ref = _tool_output_ref_for_node(graph_text, node_id)
    if not ref:
        raise ValueError(f"node not found or ref missing: {node_id}")
    if Path(ref).is_absolute() or ".." in ref:
        raise ValueError(f"invalid ref: {ref}")
    ref_path = (run_dir / ref).resolve()
    tool_outputs_dir = (run_dir / "tool_outputs").resolve()
    try:
        ref_path.relative_to(tool_outputs_dir)
    except ValueError as exc:
        raise ValueError("ref escapes tool_outputs") from exc
    if not ref_path.is_file():
        raise ValueError(f"tool output not found: {ref}")
    return ref_path.read_text(encoding="utf-8", errors="replace")


def _tool_output_ref_for_node(graph_text, node_id):
    # ref 存在独立注释行 `%% <node_id> ref: <path>`，不受节点 label 截断影响。
    comment_pattern = re.compile(
        rf"^\s*%%\s*{re.escape(node_id)}\s+ref:\s*(.+?)\s*$", re.MULTILINE
    )
    match = comment_pattern.search(str(graph_text or ""))
    return match.group(1).strip() if match else ""


def tool_search(agent, args):
    pattern = str(args.get("pattern", "")).strip()
    path = agent.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            [
                "rg", "-n", "--smart-case", "--max-count", "200",
                "--glob", "!.env*", pattern, str(path),
            ],
            cwd=agent.root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if result.returncode == 1:
            return "(no matches)"
        return f"error: search failed: {(result.stderr or result.stdout).strip() or f'rg exited with {result.returncode}'}"

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(agent.root).parts)
        and not protected_read_reason(agent, item.relative_to(agent.root))
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(agent.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_run_shell(agent, args):
    command = str(args.get("command", "")).strip()
    timeout = int(args.get("timeout", 20))
    result = agent.sandbox.run(
        command,
        cwd=agent.root,
        timeout=timeout,
        env=security.shell_env(agent),
    )
    agent._last_sandbox_metadata = agent.sandbox.audit_metadata(timed_out=result.timed_out)
    return "\n".join(
        [
            f"sandbox: {agent.sandbox.backend}",
            f"exit_code: {result.returncode}",
            "stdout:",
            result.stdout.strip() or "(empty)",
            "stderr:",
            result.stderr.strip() or "(empty)",
        ]
    )


def tool_write_file(agent, args):
    path = agent.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(agent.root)} ({len(content)} chars)"


def tool_patch_file(agent, args):
    path = agent.path(args["path"])
    old_text = str(args.get("old_text", ""))
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(agent.root)}"


def run_delegate_child(agent, args):
    if agent.depth >= agent.max_depth:
        raise ValueError("delegate depth exceeded")
    role = str(args.get("role", "")).strip()
    if role not in DELEGATE_ROLES:
        raise ValueError(f"unsupported delegate role: {role}")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")

    from .runtime import Pico

    role_config = DELEGATE_ROLES[role]
    child_task = f"{role_config['instruction']}\n\nTask:\n{task}"
    child_model_client = agent.model_client
    if getattr(agent.model_client, "supports_native_actions", False):
        child_model_client = agent.model_client.fork_for_delegate()
    child = Pico(
        model_client=child_model_client,
        workspace=agent.workspace,
        session_store=agent.session_store,
        run_store=agent.run_store,
        approval_policy="never",
        max_steps=int(args.get("max_steps", 3)),
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth + 1,
        max_depth=agent.max_depth,
        read_only=True,
        dry_run=agent.dry_run,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
        feature_flags=agent.feature_flags,
        agent_mode=role,
        parent_agent_id=agent.agent_id,
        allowed_tools=role_config["allowed_tools"],
    )
    # 委派的目标是“调查”，不是“放权执行”。
    # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
    child.memory.set_goal(child_task)
    child.memory.append_note(
        clip(agent.history_text(), 300),
        tags=("delegated_context",),
        source=agent.agent_id,
        kind="process",
    )
    child.session["memory"] = child.memory.to_dict()
    answer = child.ask(child_task)
    return {
        "role": role,
        "agent_id": child.agent_id,
        "task": task,
        "answer": answer,
    }


def format_delegate_result(result):
    return (
        f"delegate_result role={result['role']} agent_id={result['agent_id']}:\n"
        f"{result['answer']}"
    )


def tool_delegate(agent, args):
    return format_delegate_result(run_delegate_child(agent, args))


def tool_delegate_many(agent, args):
    tasks = list(args.get("tasks", []))
    results = []
    for index, task_args in enumerate(tasks, start=1):
        result = run_delegate_child(agent, task_args)
        results.append((index, result))

    lines = [f"delegate_many_result count={len(results)}"]
    for index, result in results:
        lines.extend(
            [
                f"--- child {index} role={result['role']} agent_id={result['agent_id']} ---",
                f"task: {result['task']}",
                str(result["answer"]),
            ]
        )
    return "\n".join(lines)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "read_tool_output": tool_read_tool_output,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
}
