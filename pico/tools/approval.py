"""Approval policy helpers for risky tool execution."""

import json


def approve(agent, name, args):
    if agent.read_only:
        return False
    if agent.approval_policy == "auto":
        return True
    if agent.approval_policy == "never":
        return False
    try:
        answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}
