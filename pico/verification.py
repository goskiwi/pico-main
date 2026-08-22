"""Runtime-owned verification bound to an exact workspace fingerprint."""

from __future__ import annotations

import ast
from pathlib import Path

from .sandbox import shell_argv


def changed_python_syntax_issues(agent):
    issues = []
    for relative in agent.run.evidence.changed_paths:
        if not relative.endswith(".py"):
            continue
        path = agent.workspace.resolve_path(relative)
        if not path.exists():
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeDecodeError) as exc:
            issues.append(f"{relative}:{getattr(exc, 'lineno', 1) or 1}: {exc}")
    return issues


def discover_verification_command(root):
    root = Path(root)
    if (root / "pyproject.toml").is_file():
        return "python -m pytest -q"
    if (root / "package.json").is_file():
        return "npm test -- --runInBand"
    return ""


def verify_workspace(
    *,
    root,
    command,
    sandbox,
    timeout_seconds,
    redact_text,
    fingerprint_provider,
    workspace_fingerprint,
    execution_context=None,
):
    command = str(command or "").strip()
    if not command:
        return None
    root = Path(root).resolve()
    before = str(workspace_fingerprint)
    record = {
        "command": command,
        "status": "infrastructure_error",
        "freshness": "current",
        "workspace_fingerprint": before,
        "exit_code": None,
        "output": "",
    }
    try:
        result = sandbox.run(
            shell_argv(command),
            cwd=root,
            timeout=int(timeout_seconds),
            env={},
            execution_context=execution_context,
        )
        record["exit_code"] = result.returncode
        record["output"] = redact_text(
            "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        )[:4000]
        record["status"] = "passed" if result.returncode == 0 and not result.stop_reason else "failed"
    except Exception as exc:  # noqa: BLE001 - verifier infrastructure errors are audit facts
        record["output"] = redact_text(f"{type(exc).__name__}: {exc}")
    after = fingerprint_provider()
    record["workspace_fingerprint"] = after
    if before != after:
        record["status"] = "stale"
        record["freshness"] = "stale"
    return record


def run_verification(agent, workspace_fingerprint):
    execution_context = (
        agent.run.execution_context.child()
        if agent.run.execution_context
        else None
    )
    return verify_workspace(
        root=agent.workspace.root,
        command=agent.config.verification_command,
        sandbox=agent.dependencies.sandbox,
        timeout_seconds=agent.config.run_timeout_seconds,
        redact_text=agent.redact_text,
        fingerprint_provider=lambda: agent.workspace.content_fingerprint(force=True),
        workspace_fingerprint=workspace_fingerprint,
        execution_context=execution_context,
    )
