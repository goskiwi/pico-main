"""Code-change application with optional post-Run Git delivery."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pico import Pico, PicoConfig, RunOutcome, SessionStore, Workspace


@dataclass(frozen=True, slots=True)
class CodingResult:
    outcome: RunOutcome
    delivery_status: str
    commit_sha: str = ""
    detail: str = ""


def _git(root, *args, check=True):
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=root,
            env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"},
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Git command failed: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result


def _repository_root(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("coding repository_root must be a directory")
    top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != root:
        raise ValueError("CodingWorkflow must run from the Git repository root")
    _git(root, "rev-parse", "HEAD")
    return root


def _nul_paths(result):
    return {path for path in result.stdout.split("\0") if path}


def _dirty_paths(root):
    paths = _nul_paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    for options in ((), ("--cached",)):
        fields = iter(_git(root, "diff", *options, "--name-status", "-z", "--").stdout.split("\0"))
        for status in fields:
            if not status:
                continue
            paths.add(next(fields))
            if status[0] in {"R", "C"}:
                paths.add(next(fields))
    return paths


def _default_commit_message(request):
    summary = " ".join(str(request).split()) or "update workspace"
    return ("pico: " + summary)[:72].rstrip()


class CodingWorkflow:
    """Run Pico for one code change and commit only its clean changed paths."""

    def __init__(
        self,
        model_client,
        *,
        config: PicoConfig | None = None,
        subagent_model_client_factory=None,
        command_runner=None,
        command_runner_factory=None,
    ):
        self.model_client = model_client
        self.config = config if config is not None else PicoConfig()
        if not self.config.verification_command.strip():
            raise ValueError(
                "CodingWorkflow automatic Git delivery requires verification_command"
            )
        if self.config.mode != "auto":
            raise ValueError("CodingWorkflow requires Auto mode")
        self.subagent_model_client_factory = subagent_model_client_factory
        self.command_runner = command_runner
        self.command_runner_factory = command_runner_factory

    def run(self, repository_root, request, *, commit_message=""):
        root = _repository_root(repository_root)
        dirty_before = _dirty_paths(root)
        runtime_workspace = Workspace.build(root, repo_root_override=root)
        agent = Pico(
            model_client=self.model_client,
            workspace=runtime_workspace,
            config=self.config,
            command_runner=self.command_runner,
            command_runner_factory=self.command_runner_factory,
            subagent_model_client_factory=self.subagent_model_client_factory,
            session=SessionStore(root / ".pico" / "sessions").create(
                runtime_workspace.root
            ),
        )
        outcome = agent.ask(request)
        changed_paths = outcome.changed_paths

        if outcome.status != "completed":
            return CodingResult(
                outcome,
                "skipped",
                detail=f"Run ended with status {outcome.status}",
            )
        if not changed_paths:
            return CodingResult(
                outcome,
                "skipped",
                detail="Run produced no net workspace changes",
            )

        overlap = sorted(set(changed_paths) & dirty_before)
        if overlap:
            return CodingResult(
                outcome,
                "skipped",
                detail="Pre-existing user changes share Pico paths: "
                + ", ".join(overlap),
            )

        try:
            agent.run.evidence.change_set.require_current_workspace(root)
        except RuntimeError as exc:
            return CodingResult(
                outcome,
                "failed",
                detail=f"Workspace changed before Git delivery: {exc}",
            )

        message = str(commit_message).strip() or _default_commit_message(request)
        try:
            _git(root, "add", "--", *changed_paths)
            agent.run.evidence.change_set.require_current_workspace(root)
            _git(
                root,
                "commit",
                "-m",
                message,
                "--",
                *changed_paths,
            )
            commit_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
        except RuntimeError as exc:
            _git(root, "restore", "--staged", "--", *changed_paths, check=False)
            return CodingResult(
                outcome,
                "failed",
                detail=str(exc),
            )

        return CodingResult(
            outcome,
            "committed",
            commit_sha=commit_sha,
            detail=message,
        )


__all__ = ["CodingResult", "CodingWorkflow"]
