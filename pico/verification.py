"""Runtime-owned verification bound to the Run Log mutation cursor."""

from __future__ import annotations

import os
import stat
import subprocess
from collections import deque
from pathlib import Path

from .command_runner import shell_argv
from .workspace import IGNORED_PATH_NAMES, normalize_relative_file
from .workspace_tracker import WorkspaceTracker

VERIFICATION_SNAPSHOT_MAX_ENTRIES = 20_000


def capture_changed_path_states(root, changed_paths):
    root = Path(root).resolve()
    return {
        relative: WorkspaceTracker.path_state(root / relative)
        for relative in sorted(
            {normalize_relative_file(path) for path in changed_paths}
        )
    }


def _git_workspace_state(root):
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return None
        status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude,top).pico",
                ":(exclude,top).pico/**",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None
    return tuple(status.stdout.splitlines())


def _stat_workspace_state(root):
    root = Path(root).resolve()
    snapshot = {}
    pending = deque([(root, Path())])
    observed = 0
    while pending and observed < VERIFICATION_SNAPSHOT_MAX_ENTRIES:
        directory, relative_directory = pending.popleft()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name in IGNORED_PATH_NAMES:
                continue
            relative = relative_directory / entry.name
            logical = relative.as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                snapshot[logical] = ("unavailable", type(exc).__name__)
            else:
                kind = stat.S_IFMT(metadata.st_mode)
                if stat.S_ISDIR(metadata.st_mode):
                    snapshot[logical] = ("directory", metadata.st_mode & 0o7777)
                    pending.append((Path(entry.path), relative))
                else:
                    snapshot[logical] = (
                        kind,
                        metadata.st_mode & 0o7777,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                    )
            observed += 1
            if observed >= VERIFICATION_SNAPSHOT_MAX_ENTRIES:
                break
    snapshot["<snapshot>"] = (
        "bounded",
        observed,
        bool(pending),
    )
    return snapshot


def _capture_workspace_state(root):
    root = Path(root).resolve()
    git_state = _git_workspace_state(root)
    if git_state is not None:
        return "git", git_state
    return "stat", _stat_workspace_state(root)


def _workspace_state_changes(before, after):
    before_kind, before_state = before
    after_kind, after_state = after
    if before_kind != after_kind:
        return (f"snapshot mode changed from {before_kind} to {after_kind}",)
    if before_kind == "git":
        return tuple(sorted(set(before_state) ^ set(after_state)))
    return tuple(
        path
        for path in sorted(set(before_state) | set(after_state))
        if before_state.get(path) != after_state.get(path)
    )


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
    started_workspace_state = _capture_workspace_state(root)
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
    finished_workspace_state = _capture_workspace_state(root)
    record["finished_workspace_mutation_sequence"] = after
    record["finished_changed_path_states"] = finished_changed_path_states
    reasons = []
    changed_during_verification = sorted(
        path
        for path in set(started_changed_path_states) | set(finished_changed_path_states)
        if started_changed_path_states.get(path)
        != finished_changed_path_states.get(path)
    )
    if changed_during_verification:
        reasons.append(
            "Verification command changed Runtime-tracked file contents: "
            + ", ".join(changed_during_verification)
        )
    workspace_changes = _workspace_state_changes(
        started_workspace_state,
        finished_workspace_state,
    )
    if workspace_changes:
        reasons.append(
            "Verification command changed additional workspace state: "
            + ", ".join(workspace_changes[:20])
        )
    if reasons:
        if record["status"] != "infrastructure_error":
            record["status"] = "failed"
        record["output"] = redact_text(
            "\n".join([*reasons, record["output"]]).strip()
        )[:4000]
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
