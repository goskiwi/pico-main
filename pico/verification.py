"""Runtime-owned verification bound to the Run Log mutation cursor."""

from __future__ import annotations

from pathlib import Path

from .command_runner import shell_argv
from .workspace import normalize_relative_file
from .workspace_tracker import WorkspaceTracker


def capture_changed_path_states(root, changed_paths):
    root = Path(root).resolve()
    return {
        relative: WorkspaceTracker.path_state(root / relative)
        for relative in sorted(
            {normalize_relative_file(path) for path in changed_paths}
        )
    }


def verify_workspace(
    *,
    root,
    command,
    command_runner,
    timeout_seconds,
    redact_text,
    mutation_sequence_provider,
    started_workspace_mutation_sequence,
    changed_paths,
    execution_context=None,
):
    command = str(command or "").strip()
    if not command:
        return None
    root = Path(root).resolve()
    changed_paths = tuple(changed_paths)
    before = int(started_workspace_mutation_sequence)
    started_changed_path_states = capture_changed_path_states(root, changed_paths)
    record = {
        "command": command,
        "status": "infrastructure_error",
        "started_workspace_mutation_sequence": before,
        "finished_workspace_mutation_sequence": before,
        "started_changed_path_states": started_changed_path_states,
        "finished_changed_path_states": dict(started_changed_path_states),
        "exit_code": None,
        "output": "",
    }
    try:
        result = command_runner.run(
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
        if result.infrastructure_error:
            record["status"] = "infrastructure_error"
        else:
            record["status"] = (
                "passed"
                if result.returncode == 0 and not result.stop_reason
                else "failed"
            )
    except Exception as exc:  # noqa: BLE001 - verifier infrastructure errors are audit facts
        record["output"] = redact_text(f"{type(exc).__name__}: {exc}")
    after = int(mutation_sequence_provider())
    finished_changed_path_states = capture_changed_path_states(root, changed_paths)
    record["finished_workspace_mutation_sequence"] = after
    record["finished_changed_path_states"] = finished_changed_path_states
    return record


def run_verification(agent, started_workspace_mutation_sequence):
    execution_context = (
        agent.run.execution_context.child()
        if agent.run.execution_context
        else None
    )
    return verify_workspace(
        root=agent.workspace.root,
        command=agent.config.verification_command,
        command_runner=agent.dependencies.command_runner,
        timeout_seconds=agent.config.run_timeout_seconds,
        redact_text=agent.redact_text,
        mutation_sequence_provider=lambda: (
            agent.run.evidence.last_workspace_mutation_sequence
        ),
        started_workspace_mutation_sequence=(started_workspace_mutation_sequence),
        changed_paths=agent.run.evidence.changed_paths,
        execution_context=execution_context,
    )
