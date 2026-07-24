"""Tool capability, risk, and execution policy helpers."""

from . import tools as toolkit


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


def shell_command_policy(name, args):
    if name != "run_shell":
        return None
    return toolkit.shell_command_policy((args or {}).get("command", ""))
