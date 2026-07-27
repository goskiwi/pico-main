"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import json
import shutil
import subprocess
from functools import partial
from pathlib import Path

from langchain_core.utils.function_calling import convert_to_openai_function
from pydantic import BaseModel, ConfigDict, Field, create_model

from . import security
from .delegate_scheduler import DelegateOutcome, DelegateScheduler
from .config import ALLOWED_SHELL_COMMANDS, IGNORED_PATH_NAMES
from .tool_policy import (
    DELEGATE_ROLES,
    DelegateRole,
    protected_read_reason,
    validate_read_file_spec,
)
from .workspace import WorkspaceContext, clip

ALLOWED_SHELL_PREFIX_TEXT = ", ".join(
    f"`{' '.join(command)}`" for command in ALLOWED_SHELL_COMMANDS
)


class ToolArgs(BaseModel):
    """Pydantic source of truth for one tool's model-visible arguments."""

    model_config = ConfigDict(extra="forbid")


_MISSING = object()


def _arg(annotation, default=_MISSING, **constraints):
    field = Field(**constraints) if default is _MISSING else Field(default=default, **constraints)
    return annotation, field


def _tool(model_name, description, *, capability="read", risky=False, **fields):
    return {
        "args_schema": create_model(model_name, __base__=ToolArgs, **fields),
        "capability": capability,
        "risky": risky,
        "description": description,
    }


DelegateTaskArgs = create_model(
    "DelegateTaskArgs",
    __base__=ToolArgs,
    role=_arg(
        DelegateRole,
        description="Child-agent role. Must be one of: explore, review, verify.",
    ),
    task=_arg(str, min_length=1),
    max_steps=_arg(int, 3, ge=1, le=12),
)

ReadFileSpec = create_model(
    "ReadFileSpec",
    __base__=ToolArgs,
    path=_arg(str, min_length=1, description="Workspace-relative file path."),
    start=_arg(int, 1, ge=1),
    end=_arg(int, 200, ge=1),
)


TOOL_DEFINITIONS = {
    "list_files": _tool(
        "ListFilesArgs", "List files in the workspace.", path=_arg(str, ".")
    ),
    "read_file": _tool(
        "ReadFileArgs",
        "Read one to five UTF-8 files by line range; batch related files in one call.",
        files=_arg(list[ReadFileSpec], min_length=1, max_length=5),
    ),
    "read_task_canvas": _tool(
        "ReadTaskCanvasArgs",
        "Read the active Mermaid task canvas or one archived phase; defaults to the active run.",
        run_id=_arg(str, ""),
        phase_id=_arg(str, "", description="Archived phase id such as phase_001; empty reads the active canvas."),
    ),
    "read_task_event": _tool(
        "ReadTaskEventArgs",
        "Read one offloaded tool-summary event by task-canvas node id.",
        node_id=_arg(str, min_length=1),
        run_id=_arg(str, ""),
    ),
    "read_tool_output": _tool(
        "ReadToolOutputArgs",
        "Read full saved evidence after inspecting its offloaded task event.",
        node_id=_arg(str, min_length=1),
        run_id=_arg(str, ""),
    ),
    "search": _tool(
        "SearchArgs",
        "Search the workspace with rg or a simple fallback.",
        pattern=_arg(str, min_length=1),
        path=_arg(str, "."),
    ),
    "query_repo_map": _tool(
        "QueryRepoMapArgs",
        (
            "Rank Python classes, functions, methods, imports, calls, inheritance, "
            "and test relations for a focused repository question."
        ),
        query=_arg(str, min_length=1),
        budget_tokens=_arg(int, 1200, ge=64, le=4000),
        max_results=_arg(int, 24, ge=1, le=60),
    ),
    "run_shell": _tool(
        "RunShellArgs",
        (
            "Run one allowlisted test, lint, or compile command in the mandatory "
            f"isolated Docker sandbox. Allowed prefixes: {ALLOWED_SHELL_PREFIX_TEXT}. "
            "Flags and repository-relative paths may follow; env, pipes, redirections, "
            "and compound commands are rejected."
        ),
        capability="execute",
        risky=True,
        command=_arg(
            str,
            min_length=1,
            description="Shell command to run inside the isolated workspace sandbox.",
        ),
        timeout=_arg(int, 20, ge=1, le=120),
    ),
    "write_file": _tool(
        "WriteFileArgs",
        "Write a text file.",
        capability="write",
        risky=True,
        path=_arg(
            str,
            min_length=1,
            description="Workspace-relative path to create or replace.",
        ),
        content=_arg(
            str,
            description=(
                "Complete file content to write, including imports and final newline "
                "when appropriate."
            ),
        ),
    ),
    "patch_file": _tool(
        "PatchFileArgs",
        "Replace one exact text block in a file.",
        capability="write",
        risky=True,
        path=_arg(str, min_length=1, description="Workspace-relative path to edit."),
        old_text=_arg(
            str,
            min_length=1,
            description=(
                "Exact existing file text without read_file metadata or display line "
                "numbers; it must occur exactly once."
            ),
        ),
        new_text=_arg(str, description="Complete replacement for old_text."),
    ),
    "delegate": _tool(
        "DelegateArgs",
        "Ask a bounded read-only child agent to investigate using explore, review, or verify.",
        capability="delegate",
        role=_arg(
            DelegateRole,
            description="Child-agent role. Must be one of: explore, review, verify.",
        ),
        task=_arg(str, min_length=1),
        max_steps=_arg(int, 3, ge=1, le=12),
    ),
    "delegate_many": _tool(
        "DelegateManyArgs",
        "Ask several bounded read-only child agents to investigate separate tasks.",
        capability="delegate",
        tasks=_arg(list[DelegateTaskArgs], min_length=1, max_length=6),
    ),
}
SUBMIT_FINAL_DEFINITION = _tool(
    "SubmitFinalArgs",
    "Finish the task only after all required workspace work is complete.",
    answer=_arg(
        str,
        min_length=1,
        description="Concise final answer describing completed work and verification.",
    ),
)


def _tool_spec(definition):
    return {
        "args_schema": definition["args_schema"],
        "capability": definition["capability"],
        "risky": definition["risky"],
        "description": definition["description"],
    }


BASE_TOOL_NAMES = (
    "list_files",
    "read_file",
    "read_task_canvas",
    "read_task_event",
    "read_tool_output",
    "search",
    "query_repo_map",
    "run_shell",
    "write_file",
    "patch_file",
)
BASE_TOOL_SPECS = {name: _tool_spec(TOOL_DEFINITIONS[name]) for name in BASE_TOOL_NAMES}
DELEGATE_TOOL_SPEC = _tool_spec(TOOL_DEFINITIONS["delegate"])
DELEGATE_MANY_TOOL_SPEC = _tool_spec(TOOL_DEFINITIONS["delegate_many"])

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
    if not agent.feature_enabled("repo_map"):
        tools.pop("query_repo_map", None)
    if agent.allowed_tools is not None:
        allowed = set(agent.allowed_tools)
        tools = {name: tool for name, tool in tools.items() if name in allowed}
    return tools

def schema_field_display(spec, *, required):
    field_type = {
        "array": "list",
        "integer": "int",
        "string": "str",
    }.get(str(spec.get("type", "")), str(spec.get("type", "value")))
    if required:
        return field_type
    if "default" in spec:
        return f"{field_type}={spec['default']!r}"
    return field_type


def schema_display(args_schema):
    schema = args_schema.model_json_schema()
    required = set(schema.get("required", []))
    return {
        name: schema_field_display(spec, required=name in required)
        for name, spec in schema.get("properties", {}).items()
    }


def responses_action_tools(tool_registry):
    """Build strict Responses function definitions."""
    definitions = [
        _flat_action_definition(name, tool)
        for name, tool in sorted(dict(tool_registry).items())
    ]
    definitions.append(_flat_action_definition("submit_final", SUBMIT_FINAL_DEFINITION))
    return definitions


def _flat_action_definition(name, definition):
    return {
        "type": "function",
        "name": name,
        "description": str(definition.get("description", "")),
        "parameters": function_schema(definition["args_schema"]),
        "strict": True,
    }


def function_schema(args_schema):
    """Convert a Pydantic model to the function schema shared by Pico tools."""
    function = convert_to_openai_function(args_schema, strict=True)
    return _render_runtime_defaults(function["parameters"])


def _render_runtime_defaults(schema):
    rendered = {}
    for key, value in schema.items():
        if key == "default":
            continue
        if isinstance(value, dict):
            rendered[key] = _render_runtime_defaults(value)
        elif isinstance(value, list):
            rendered[key] = [
                _render_runtime_defaults(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            rendered[key] = value
    if "default" in schema:
        note = f"Use {schema['default']!r} when the default behavior is intended."
        rendered["description"] = " ".join(
            item for item in (str(rendered.get("description", "")).strip(), note) if item
        )
    if rendered.get("type") == "object" and isinstance(rendered.get("properties"), dict):
        rendered["required"] = list(rendered["properties"])
    return rendered


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


def _read_file_segment(path, start, end, root):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return (
        f"=== read_file metadata: {path.relative_to(root)}; "
        "header and line numbers are not file content ===\n"
        f"{body}"
    )


def tool_read_file(agent, args):
    segments = []
    for index, file_args in enumerate(args["files"], start=1):
        path, start, end = validate_read_file_spec(agent, file_args, label=f"files[{index}]")
        segments.append(_read_file_segment(path, start, end, agent.root))
    return "\n\n".join(segments)


def _artifact_run_id(agent, args):
    run_id = str(args.get("run_id", "")).strip()
    if not run_id:
        task_state = agent.current_task_state
        run_id = str(task_state.run_id if task_state is not None else "")
    if not run_id:
        raise ValueError("run_id is required when no current run is active")
    return run_id


def tool_read_task_canvas(agent, args):
    run_id = _artifact_run_id(agent, args)
    return agent.run_store.task_canvas_text(
        run_id,
        phase_id=str(args.get("phase_id", "")).strip(),
    )


def tool_read_task_event(agent, args):
    run_id = _artifact_run_id(agent, args)
    node_id = str(args["node_id"]).strip()
    event = agent.run_store.read_offload_event(run_id, node_id)
    return json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True)


def tool_read_tool_output(agent, args):
    node_id = str(args["node_id"]).strip()
    run_id = _artifact_run_id(agent, args)

    run_dir = agent.run_store.run_dir(run_id).resolve()
    event = agent.run_store.read_offload_event(run_id, node_id)
    ref = str(event.get("result_ref", "")).strip()
    if not ref:
        raise ValueError(f"result_ref missing for node: {node_id}")
    if Path(ref).is_absolute() or ".." in ref:
        raise ValueError(f"invalid ref: {ref}")
    ref_path = (run_dir / ref).resolve()
    refs_dir = agent.run_store.refs_dir(run_id).resolve()
    try:
        ref_path.relative_to(refs_dir)
    except ValueError as exc:
        raise ValueError("ref escapes refs") from exc
    if not ref_path.is_file():
        raise ValueError(f"reference not found: {ref}")
    return ref_path.read_text(encoding="utf-8", errors="replace")


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
            root_prefix = f"{agent.root}{Path('/')}"
            return "\n".join(
                line.removeprefix(root_prefix) for line in result.stdout.splitlines()
            ).strip()
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


def tool_query_repo_map(agent, args):
    rendered = agent.repo_map.render(
        str(args["query"]).strip(),
        budget_tokens=int(args.get("budget_tokens", 1200)),
        max_results=int(args.get("max_results", 24)),
        token_counter=agent.count_tokens,
    )
    details = rendered.details
    evidence = (
        "repo_map_stats: "
        f"files={details.get('parsed_files', 0)} "
        f"nodes={details.get('graph_nodes', 0)} "
        f"edges={details.get('graph_edges', 0)} "
        f"selected={details.get('selected_count', 0)} "
        f"cache_hits={details.get('cache_hits', 0)} "
        f"cache_misses={details.get('cache_misses', 0)}"
    )
    return f"{rendered.text}\n\n{evidence}".strip()


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
    agent._assert_workspace_root()
    role = str(args.get("role", "")).strip()
    if role not in DELEGATE_ROLES:
        raise ValueError(f"unsupported delegate role: {role}")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")

    from .runtime import Pico

    role_config = DELEGATE_ROLES[role]
    child_task = f"{role_config['instruction']}\n\nTask:\n{task}"
    child_model_client = agent.model_client.fork_for_delegate()
    child_feature_flags = dict(agent.feature_flags)
    # Delegate children are intentionally read-only. Requiring a workspace
    # change would reject their valid investigation final answers forever.
    child_feature_flags["require_workspace_change"] = False
    child_workspace = WorkspaceContext.build(
        agent.root,
        repo_root_override=agent.root,
    )
    child = Pico(
        model_client=child_model_client,
        workspace=child_workspace,
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
        feature_flags=child_feature_flags,
        trust_project=agent.trust_project,
        agent_mode=role,
        parent_agent_id=agent.agent_id,
        allowed_tools=role_config["allowed_tools"],
        trace_sink=agent.trace_sink,
    )
    child._assert_workspace_root()
    # 委派的目标是“调查”，不是“放权执行”。
    # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
    child.memory.set_goal(child_task)
    child.memory.append_note(
        clip(agent.task_context_text(), 300),
        tags=("delegated_context",),
        source=agent.agent_id,
    )
    child.session["memory"] = child.memory.to_dict()
    answer = child.ask(child_task)
    return {
        "role": role,
        "agent_id": child.agent_id,
        "task": task,
        "answer": answer,
        "status": child.current_task_state.status,
        "stop_reason": child.current_task_state.stop_reason,
    }


def format_delegate_result(outcome: DelegateOutcome):
    """Render a scheduler outcome; scheduling itself remains outside tools."""
    if outcome.status == "ok" and outcome.result is not None:
        result = outcome.result
        return (
            f"delegate_result role={result['role']} agent_id={result['agent_id']}:\n"
            f"{result['answer']}"
        )
    role = str(outcome.spec.get("role", "unknown"))
    return (
        f"delegate_result role={role} status={outcome.status}:\n"
        f"{outcome.error or 'delegate did not return a result'}"
    )


def _record_delegate_outcomes(agent, outcomes, *, requested_count):
    """Keep complete scheduler evidence separate from the clipped text result."""
    items = []
    for outcome in outcomes:
        result = dict(outcome.result or {})
        items.append(
            {
                "index": int(outcome.index),
                "role": str(result.get("role") or outcome.spec.get("role", "")).strip(),
                "status": str(outcome.status),
                "agent_id": str(result.get("agent_id", "")).strip(),
                "child_status": str(result.get("status", "")).strip(),
                "stop_reason": str(result.get("stop_reason", "")).strip(),
            }
        )
    completed_count = sum(item["status"] == "ok" for item in items)
    agent._delegate_outcome_metadata = {
        "requested_count": int(requested_count),
        "completed_count": completed_count,
        "failed_count": int(requested_count) - completed_count,
        "items": items,
    }


def tool_delegate(agent, args):
    outcome = DelegateScheduler(agent).run([args])[0]
    _record_delegate_outcomes(agent, [outcome], requested_count=1)
    return format_delegate_result(outcome)


def tool_delegate_many(agent, args):
    tasks = list(args.get("tasks", []))
    outcomes = DelegateScheduler(agent).run(tasks)
    _record_delegate_outcomes(agent, outcomes, requested_count=len(tasks))

    lines = [f"delegate_many_result count={len(outcomes)}"]
    for outcome in outcomes:
        if outcome.status == "ok" and outcome.result is not None:
            result = outcome.result
            lines.extend(
                [
                    f"--- child {outcome.index} role={result['role']} agent_id={result['agent_id']} ---",
                    f"task: {result['task']}",
                    str(result["answer"]),
                ]
            )
            continue
        role = str(outcome.spec.get("role", "unknown"))
        lines.extend(
            [
                f"--- child {outcome.index} role={role} status={outcome.status} ---",
                f"{outcome.error or 'delegate did not return a result'}",
            ]
        )
    return "\n".join(lines)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "read_task_canvas": tool_read_task_canvas,
    "read_task_event": tool_read_task_event,
    "read_tool_output": tool_read_tool_output,
    "search": tool_search,
    "query_repo_map": tool_query_repo_map,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
}
