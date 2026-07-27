"""Tool capability, argument, and execution-policy helpers."""

import re
import shlex
from enum import Enum

from pydantic import ValidationError

from .config import (
    ALLOWED_SHELL_COMMANDS,
    PROTECTED_WRITE_FILENAMES,
    PROTECTED_WRITE_PATH_PARTS,
    _DANGEROUS_SHELL_PATTERNS_RAW,
)


DANGEROUS_SHELL_PATTERNS = tuple(
    (reason, re.compile(pattern)) for reason, pattern in _DANGEROUS_SHELL_PATTERNS_RAW
)


class DelegateRole(str, Enum):
    EXPLORE = "explore"
    REVIEW = "review"
    VERIFY = "verify"


DELEGATE_ROLES = {
    DelegateRole.EXPLORE.value: {
        "allowed_tools": ("query_repo_map", "list_files", "read_file", "search"),
        "instruction": "Explore the repository for facts relevant to the task. Do not propose edits unless asked to report risks.",
    },
    DelegateRole.REVIEW.value: {
        "allowed_tools": ("query_repo_map", "list_files", "read_file", "search"),
        "instruction": "Review the relevant code for bugs, regressions, missing tests, and safety issues. Report findings with file references when possible.",
    },
    DelegateRole.VERIFY.value: {
        "allowed_tools": ("query_repo_map", "list_files", "read_file", "search"),
        "instruction": "Verify the current state by inspecting files, test output, and evidence. Report what is confirmed and what remains uncertain.",
    },
}


def tool_capability(tool):
    if not tool:
        return ""
    return str(tool.get("capability", "write" if tool.get("risky") else "read"))


def tool_risk_level(tool):
    capability = tool_capability(tool)
    if capability in {"write", "execute"}:
        return "high"
    if capability == "delegate":
        return "medium"
    return "low"


def tool_permission_error(agent, tool):
    capability = tool_capability(tool)
    if agent.read_only and capability != "read":
        return {
            "code": "capability_denied",
            "security_event_type": "read_only_block",
            "message": f"error: permission denied for {capability} capability in read-only mode",
        }
    return None


def dry_run_tool_result(name, args):
    args = args or {}
    if name == "run_shell":
        return f"dry_run: would run shell command: {args.get('command', '')}"
    if name == "write_file":
        content = str(args.get("content", ""))
        return f"dry_run: would write {args.get('path', '')} ({len(content)} chars)"
    if name == "patch_file":
        return f"dry_run: would patch {args.get('path', '')}"
    return f"dry_run: would execute {name}"


def shell_policy_metadata(policy):
    if not policy:
        return {
            "shell_allowlisted": None,
            "shell_policy_reason": "",
            "shell_allowlist_match": "",
        }
    return {
        "shell_allowlisted": bool(policy.get("allowed")),
        "shell_policy_reason": str(policy.get("reason", "")),
        "shell_allowlist_match": str(policy.get("matched_prefix", "")),
    }


def detect_dangerous_shell_command(command):
    normalized = " ".join(str(command or "").strip().split())
    for reason, pattern in DANGEROUS_SHELL_PATTERNS:
        if pattern.search(normalized):
            return reason
    return ""


def _shell_command_policy(command):
    command = str(command or "").strip()
    if not command:
        return {"allowed": False, "matched_prefix": "", "reason": "empty_command"}
    if any(marker in command for marker in (";", "&", "|", ">", "<", "$(", "`", "\n", "\r")):
        return {"allowed": False, "matched_prefix": "", "reason": "shell_composition"}
    try:
        parts = shlex.split(command)
    except ValueError:
        return {"allowed": False, "matched_prefix": "", "reason": "parse_error"}
    if not parts:
        return {"allowed": False, "matched_prefix": "", "reason": "empty_command"}
    for allowed in ALLOWED_SHELL_COMMANDS:
        if tuple(parts[: len(allowed)]) == allowed:
            return {
                "allowed": True,
                "matched_prefix": " ".join(allowed),
                "reason": "allowlisted",
            }
    return {"allowed": False, "matched_prefix": "", "reason": "not_allowlisted"}


def shell_command_policy(name, args):
    if name != "run_shell":
        return None
    return _shell_command_policy((args or {}).get("command", ""))


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
    if path.name.startswith(".env") and path.name != ".env.example":
        return f"protected secret-like file: {path.name}"
    return ""


def _validation_location(parts):
    location = ""
    for part in parts:
        if isinstance(part, int):
            location += f"[{part + 1}]"
        else:
            location += ("." if location else "") + str(part)
    return location


def _validation_error_message(error):
    location = _validation_location(error.get("loc", ()))
    error_type = str(error.get("type", ""))
    context = error.get("ctx", {})
    if error_type == "missing":
        return f"missing required argument: {location}"
    if error_type == "extra_forbidden":
        return f"unexpected argument: {location}"
    if error_type == "string_type":
        return f"{location} must be a string"
    if error_type in {"int_type", "int_parsing"}:
        return f"{location} must be an integer"
    if error_type == "list_type":
        return f"{location} must be a list"
    if error_type == "string_too_short" and int(context.get("min_length", 0)) == 1:
        return f"{location} must not be empty"
    if error_type == "too_short":
        return f"{location} must contain at least {context['min_length']} item(s)"
    if error_type == "too_long":
        return f"{location} must contain at most {context['max_length']} item(s)"
    if error_type == "greater_than_equal":
        return f"{location} must be >= {context['ge']}"
    if error_type == "less_than_equal":
        return f"{location} must be <= {context['le']}"
    return f"{location}: {error.get('msg', 'invalid value')}".strip(": ")


def validate_read_file_spec(agent, file_args, *, label):
    raw_path = file_args["path"]
    path = agent.path(raw_path)
    protected_reason = protected_read_reason(agent, raw_path)
    if protected_reason:
        raise ValueError(f"protected read path blocked: {protected_reason}")
    if not path.is_file():
        raise ValueError(f"{label}.path is not a file")
    start = int(file_args.get("start", 1))
    end = int(file_args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError(f"{label} has an invalid line range")
    return path, start, end


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


def validate_tool(agent, name, args, tool):
    args = args or {}
    args_schema = tool.get("args_schema") if tool else None
    if args_schema is not None:
        try:
            args_schema.model_validate(args)
        except ValidationError as exc:
            error = exc.errors(include_url=False)[0]
            raise ValueError(_validation_error_message(error)) from exc

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        for index, file_args in enumerate(args["files"], start=1):
            validate_read_file_spec(agent, file_args, label=f"files[{index}]")
        return

    if name in {"read_task_canvas", "read_task_event", "read_tool_output"}:
        run_id = str(args.get("run_id", "")).strip()
        if run_id and (".." in run_id or "/" in run_id or "\\" in run_id):
            raise ValueError("invalid run_id")
        phase_id = str(args.get("phase_id", "")).strip()
        if phase_id and not re.fullmatch(r"phase_\d{3,}", phase_id):
            raise ValueError("invalid phase_id")
        if name != "read_task_canvas":
            node_id = str(args.get("node_id", "")).strip()
            if not node_id:
                raise ValueError("node_id must not be empty")
            if ".." in node_id or "/" in node_id or "\\" in node_id:
                raise ValueError("invalid node_id")
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
            raise ValueError(
                f"old_text must occur exactly once, found {count}; use exact file content "
                "without the read_file metadata header or display line numbers"
            )
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
