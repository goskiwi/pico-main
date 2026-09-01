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
GIT_SNAPSHOT_TIMEOUT_SECONDS = 10


class RepositorySnapshotError(RuntimeError):
    """The Runtime could not establish one trustworthy repository baseline."""


def capture_changed_path_states(root, changed_paths):
    root = Path(root).resolve()
    return {
        relative: WorkspaceTracker.path_state(root / relative)
        for relative in sorted(
            {normalize_relative_file(path) for path in changed_paths}
        )
    }


def _run_git(root, args, *, text=False, allow_failure=False):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=text,
            timeout=GIT_SNAPSHOT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositorySnapshotError(
            f"Git repository observation failed: {type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode and not allow_failure:
        detail = result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        raise RepositorySnapshotError(
            "Git repository observation failed: " + detail.strip()
        )
    return result


def _nul_paths(payload):
    return tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in bytes(payload).split(b"\0")
        if item
    )


def _untracked_path_state(path):
    path = Path(path)
    try:
        metadata = path.lstat()
        if path.is_symlink():
            return ("symlink", os.readlink(path))
        if stat.S_ISREG(metadata.st_mode):
            return (
                "file",
                metadata.st_mode & 0o7777,
                WorkspaceTracker.path_state(path),
            )
        return (
            "other",
            stat.S_IFMT(metadata.st_mode),
            metadata.st_mode & 0o7777,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    except OSError as exc:
        return ("unavailable", type(exc).__name__)


def _git_repository_state(root):
    probe = _run_git(
        root,
        ("rev-parse", "--is-inside-work-tree"),
        text=True,
        allow_failure=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None

    head = _run_git(
        root,
        ("rev-parse", "--verify", "HEAD"),
        text=True,
        allow_failure=True,
    )
    if head.returncode != 0:
        # An unborn repository has no stable HEAD baseline. Use the same bounded
        # fallback as a non-Git workspace instead of growing a second protocol.
        return None
    head_ref = _run_git(
        root,
        ("symbolic-ref", "-q", "HEAD"),
        text=True,
        allow_failure=True,
    )
    pathspec = (
        "--",
        ".",
        ":(exclude,top).pico",
        ":(exclude,top).pico/**",
    )
    common_diff = ("--no-ext-diff", "--no-textconv", "--binary")
    total_diff = _run_git(
        root,
        ("diff", *common_diff, "HEAD", *pathspec),
    ).stdout
    total_paths = _nul_paths(
        _run_git(
            root,
            ("diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", *pathspec),
        ).stdout
    )
    staged_diff = _run_git(
        root,
        ("diff", *common_diff, "--cached", "HEAD", *pathspec),
    ).stdout
    staged_paths = _nul_paths(
        _run_git(
            root,
            (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--name-only",
                "-z",
                "--cached",
                "HEAD",
                *pathspec,
            ),
        ).stdout
    )
    untracked_paths = _nul_paths(
        _run_git(
            root,
            (
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                *pathspec,
            ),
        ).stdout
    )
    return {
        "head_oid": head.stdout.strip(),
        "head_ref": head_ref.stdout.strip() if head_ref.returncode == 0 else "",
        "total_diff": total_diff,
        "total_paths": total_paths,
        "staged_diff": staged_diff,
        "staged_paths": staged_paths,
        "untracked": {
            relative: _untracked_path_state(root / relative)
            for relative in untracked_paths
        },
    }


def _capture_bounded_filesystem_state(root):
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
    if pending:
        raise RepositorySnapshotError(
            "bounded filesystem observation exceeded "
            f"{VERIFICATION_SNAPSHOT_MAX_ENTRIES} entries"
        )
    return snapshot


def capture_repository_state(root):
    """Capture repository-visible state without persisting the fingerprint."""

    root = Path(root).resolve()
    git_state = _git_repository_state(root)
    if git_state is not None:
        return "git", git_state
    return "stat", _capture_bounded_filesystem_state(root)


def repository_state_changes(before, after):
    before_kind, before_state = before
    after_kind, after_state = after
    if before_kind != after_kind:
        return (f"snapshot mode changed from {before_kind} to {after_kind}",)
    if before_kind == "git":
        changes = set()
        if (
            before_state["head_oid"] != after_state["head_oid"]
            or before_state["head_ref"] != after_state["head_ref"]
        ):
            changes.add("<HEAD>")
        if before_state["total_diff"] != after_state["total_diff"]:
            changes.update(before_state["total_paths"])
            changes.update(after_state["total_paths"])
        if before_state["staged_diff"] != after_state["staged_diff"]:
            changes.update(before_state["staged_paths"])
            changes.update(after_state["staged_paths"])
        before_untracked = before_state["untracked"]
        after_untracked = after_state["untracked"]
        changes.update(
            path
            for path in set(before_untracked) | set(after_untracked)
            if before_untracked.get(path) != after_untracked.get(path)
        )
        return tuple(sorted(changes))
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
        started_workspace_state = capture_repository_state(root)
    except RepositorySnapshotError as exc:
        record["output"] = redact_text(str(exc))[:4000]
        return record
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
    try:
        finished_workspace_state = capture_repository_state(root)
    except RepositorySnapshotError as exc:
        finished_workspace_state = None
        record["status"] = "infrastructure_error"
        record["output"] = redact_text(
            "\n".join([str(exc), record["output"]]).strip()
        )[:4000]
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
    workspace_changes = (
        repository_state_changes(started_workspace_state, finished_workspace_state)
        if finished_workspace_state is not None
        else ()
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
