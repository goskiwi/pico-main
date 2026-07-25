"""Live-model benchmark for Repo Map task success and conflict-safe Undo recovery."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import real_benchmark_evidence as evidence
from .common import git_value, safe_mean, safe_ratio, utc_timestamp
from .real_benchmark import (
    _evaluation_snapshot_id,
    _fixture_snapshot_id,
    _relative_file,
    _variant_feature_flags,
    build_real_model_client,
)
from pico.cli import DEFAULT_OPENAI_MODEL, _load_workspace_env
from pico.run_store import RunStore
from pico.run_undo import RunUndoError, restore_run
from pico.runtime import Pico
from pico.sandbox import DockerSandbox, DockerSandboxConfig, SandboxResult
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


RELIABILITY_BENCHMARK_SCHEMA_VERSION = 1
RELIABILITY_ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_RELIABILITY_BENCHMARK_PATH = Path(
    "benchmarks/reliability_tasks_v1.json"
)
DEFAULT_RELIABILITY_ARTIFACT_PATH = Path(
    "artifacts/reliability-benchmark-v1-live-3x.json"
)
DEFAULT_RELIABILITY_REPORT_PATH = Path(
    "docs/metrics/reliability-benchmark-v1-live-3x.md"
)
DEFAULT_RELIABILITY_WORKSPACE_ROOT = Path(
    "artifacts/reliability-benchmark-workspaces"
)
SUPPORTED_MODES = ("task_success", "undo_recovery")
REQUIRED_TASK_KEYS = (
    "id",
    "category",
    "mode",
    "prompt",
    "fixture_repo",
    "allowed_tools",
    "step_budget",
    "verifier_files",
    "verifier_command",
    "expected_agent_changes",
)
IGNORED_SNAPSHOT_PARTS = {
    ".benchmark_hidden",
    ".git",
    ".pico",
    ".pytest_cache",
    "__pycache__",
}


def _safe_relative(path, root, *, label):
    return str(_relative_file(path, root, label=label))


def validate_reliability_benchmark(payload, repo_root):
    if not isinstance(payload, dict):
        raise ValueError("reliability benchmark must be an object")
    if (
        int(payload.get("schema_version", 0))
        != RELIABILITY_BENCHMARK_SCHEMA_VERSION
    ):
        raise ValueError("unsupported reliability benchmark schema_version")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("reliability benchmark tasks must be a non-empty list")

    repo_root = Path(repo_root).resolve()
    seen_ids = set()
    normalized_tasks = []
    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            raise ValueError(f"task at index {index} must be an object")
        missing = [key for key in REQUIRED_TASK_KEYS if key not in raw_task]
        if missing:
            raise ValueError(
                f"task {raw_task.get('id', index)!r} missing: "
                + ", ".join(missing)
            )
        task = dict(raw_task)
        task_id = str(task["id"]).strip()
        if not task_id or task_id in seen_ids:
            raise ValueError(f"empty or duplicate task id: {task_id!r}")
        seen_ids.add(task_id)
        task["id"] = task_id
        task["category"] = str(task["category"]).strip()
        task["mode"] = str(task["mode"]).strip()
        if task["mode"] not in SUPPORTED_MODES:
            raise ValueError(
                f"task {task_id!r} mode must be one of: "
                + ", ".join(SUPPORTED_MODES)
            )
        task["prompt"] = str(task["prompt"]).strip()
        task["fixture_repo"] = str(task["fixture_repo"]).strip()
        fixture_root = (repo_root / task["fixture_repo"]).resolve()
        try:
            fixture_root.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(
                f"task {task_id!r} fixture escapes repository"
            ) from exc
        if not fixture_root.is_dir():
            raise ValueError(f"task {task_id!r} fixture does not exist")

        task["allowed_tools"] = [
            str(name).strip() for name in task["allowed_tools"]
        ]
        if not task["allowed_tools"] or any(
            not name for name in task["allowed_tools"]
        ):
            raise ValueError(f"task {task_id!r} allowed_tools are invalid")
        task["step_budget"] = int(task["step_budget"])
        if task["step_budget"] < 1:
            raise ValueError(f"task {task_id!r} step_budget must be positive")
        task["verifier_command"] = str(task["verifier_command"]).strip()
        if not task["verifier_command"]:
            raise ValueError(
                f"task {task_id!r} verifier_command must not be empty"
            )

        verifier_files = []
        for raw_file in task["verifier_files"]:
            item = dict(raw_file)
            source = _safe_relative(
                item.get("source", ""),
                repo_root,
                label="verifier source",
            )
            source_path = repo_root / source
            if not source_path.is_file():
                raise ValueError(
                    f"task {task_id!r} verifier source does not exist: "
                    f"{source}"
                )
            target = _safe_relative(
                item.get("target", ""),
                fixture_root,
                label="verifier target",
            )
            verifier_files.append({"source": source, "target": target})
        task["verifier_files"] = verifier_files

        expected_changes = []
        for path in task["expected_agent_changes"]:
            expected_changes.append(
                _safe_relative(
                    path,
                    fixture_root,
                    label="expected agent change",
                )
            )
        if not expected_changes:
            raise ValueError(
                f"task {task_id!r} expected_agent_changes must not be empty"
            )
        task["expected_agent_changes"] = sorted(set(expected_changes))

        preexisting_edits = []
        edited_paths = set()
        for raw_edit in task.get("preexisting_edits", []):
            edit = dict(raw_edit)
            relative = _safe_relative(
                edit.get("path", ""),
                fixture_root,
                label="preexisting edit",
            )
            if relative in edited_paths:
                raise ValueError(
                    f"task {task_id!r} repeats preexisting edit: {relative}"
                )
            edited_paths.add(relative)
            append = str(edit.get("append", ""))
            if not append:
                raise ValueError(
                    f"task {task_id!r} preexisting edit must append content"
                )
            if not (fixture_root / relative).is_file():
                raise ValueError(
                    f"task {task_id!r} preexisting path is not a file: "
                    f"{relative}"
                )
            preexisting_edits.append({"path": relative, "append": append})
        task["preexisting_edits"] = preexisting_edits

        dirty_paths = []
        for path in task.get("dirty_paths", []):
            dirty_paths.append(
                _safe_relative(
                    path,
                    fixture_root,
                    label="dirty path",
                )
            )
        task["dirty_paths"] = sorted(set(dirty_paths))
        if not set(task["dirty_paths"]).issubset(edited_paths):
            raise ValueError(
                f"task {task_id!r} dirty_paths must have preexisting edits"
            )
        if task["mode"] == "task_success" and task["dirty_paths"]:
            raise ValueError(
                f"task {task_id!r} task_success mode cannot declare dirty_paths"
            )
        normalized_tasks.append(task)

    normalized = dict(payload)
    normalized["tasks"] = normalized_tasks
    return normalized


def load_reliability_benchmark(
    path=DEFAULT_RELIABILITY_BENCHMARK_PATH,
    repo_root=None,
):
    path = Path(path)
    root = Path(repo_root or path.parent.parent).resolve()
    return validate_reliability_benchmark(
        json.loads(path.read_text(encoding="utf-8")),
        root,
    )


def _workspace_hashes(root):
    root = Path(root)
    hashes = {}
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        relative = path.relative_to(root)
        if any(part in IGNORED_SNAPSHOT_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            payload = f"symlink:{path.readlink()}".encode()
        elif path.is_file():
            payload = path.read_bytes()
        else:
            continue
        hashes[str(relative)] = hashlib.sha256(payload).hexdigest()
    return hashes


def _snapshot_digest(hashes):
    payload = json.dumps(
        hashes,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _changed_paths(before, after):
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _path_hash_evidence(paths, *, pristine, pre_run, after_agent, after_undo):
    return {
        path: {
            "pristine": pristine.get(path),
            "pre_run": pre_run.get(path),
            "after_agent": after_agent.get(path),
            "after_undo": after_undo.get(path),
        }
        for path in sorted(set(paths))
    }


def summarize_reliability_rows(rows):
    rows = list(rows)
    task_rows = {}
    for row in rows:
        task_rows.setdefault(row["task_id"], []).append(row)

    tasks = {}
    for task_id, selected in sorted(task_rows.items()):
        tasks[task_id] = {
            "category": selected[0]["category"],
            "mode": selected[0]["mode"],
            "attempt_count": len(selected),
            "passed": sum(bool(row["passed"]) for row in selected),
            "pass_rate": safe_ratio(
                sum(bool(row["passed"]) for row in selected),
                len(selected),
            ),
            "avg_tool_steps": safe_mean(
                row["tool_steps"] for row in selected
            ),
            "avg_model_calls": safe_mean(
                row["model_calls"] for row in selected
            ),
            "total_input_tokens": sum(
                row["input_tokens"] for row in selected
            ),
            "total_output_tokens": sum(
                row["output_tokens"] for row in selected
            ),
            "avg_duration_ms": safe_mean(
                row["total_duration_ms"] for row in selected
            ),
            "exact_restorations": sum(
                bool(row["recovery"]["exact_restoration"])
                for row in selected
            ),
            "dirty_preservations": sum(
                bool(row["recovery"]["dirty_preserved"])
                for row in selected
                if row["dirty_paths"]
            ),
        }

    recovery_rows = [
        row for row in rows if row["mode"] == "undo_recovery"
    ]
    dirty_rows = [row for row in recovery_rows if row["dirty_paths"]]
    return {
        "attempt_count": len(rows),
        "passed": sum(bool(row["passed"]) for row in rows),
        "pass_rate": safe_ratio(
            sum(bool(row["passed"]) for row in rows),
            len(rows),
        ),
        "recovery_attempt_count": len(recovery_rows),
        "recovered": sum(
            bool(row["recovery"]["passed"]) for row in recovery_rows
        ),
        "recovery_rate": safe_ratio(
            sum(bool(row["recovery"]["passed"]) for row in recovery_rows),
            len(recovery_rows),
        ),
        "exact_restoration_rate": safe_ratio(
            sum(
                bool(row["recovery"]["exact_restoration"])
                for row in recovery_rows
            ),
            len(recovery_rows),
        ),
        "dirty_preservation_rate": safe_ratio(
            sum(
                bool(row["recovery"]["dirty_preserved"])
                for row in dirty_rows
            ),
            len(dirty_rows),
        ),
        "total_input_tokens": sum(row["input_tokens"] for row in rows),
        "total_output_tokens": sum(row["output_tokens"] for row in rows),
        "model_failures": sum(row["model_failures"] for row in rows),
        "model_action_rejections": sum(
            row["model_action_rejections"] for row in rows
        ),
        "trace_parse_errors": sum(
            len(row["trace_parse_errors"]) for row in rows
        ),
        "workspace_isolation_failures": sum(
            not row["workspace_isolation"]["ok"] for row in rows
        ),
        "avg_tool_steps": safe_mean(row["tool_steps"] for row in rows),
        "avg_model_calls": safe_mean(row["model_calls"] for row in rows),
        "avg_duration_ms": safe_mean(
            row["total_duration_ms"] for row in rows
        ),
        "tasks": tasks,
    }


def render_reliability_markdown(artifact):
    summary = artifact["summary"]
    runtime = artifact["runtime"]
    lines = [
        "# Pico reliability benchmark V1",
        "",
        "## Result",
        "",
        (
            f"- Overall: **{summary['passed']}/{summary['attempt_count']} "
            f"({summary['pass_rate']:.1%})** attempts satisfied their "
            "pre-registered acceptance criteria."
        ),
        (
            f"- Undo recovery: **{summary['recovered']}/"
            f"{summary['recovery_attempt_count']} "
            f"({summary['recovery_rate']:.1%})**."
        ),
        (
            "- Exact whole-workspace restoration: "
            f"**{summary['exact_restoration_rate']:.1%}**."
        ),
        (
            "- Pre-existing dirty-file preservation: "
            f"**{summary['dirty_preservation_rate']:.1%}**."
        ),
        (
            "- Recorded model failures / Action rejections / trace parse "
            "errors / workspace-isolation failures: "
            f"**{summary['model_failures']} / "
            f"{summary['model_action_rejections']} / "
            f"{summary['trace_parse_errors']} / "
            f"{summary['workspace_isolation_failures']}**."
        ),
        "",
        "## Per-scenario metrics",
        "",
        (
            "| Scenario | Mode | Passed | Avg tool steps | Avg model calls "
            "| Tokens in/out | Avg duration |"
        ),
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for task_id, metrics in summary["tasks"].items():
        lines.append(
            f"| `{task_id}` | {metrics['mode']} | "
            f"{metrics['passed']}/{metrics['attempt_count']} | "
            f"{metrics['avg_tool_steps']:.2f} | "
            f"{metrics['avg_model_calls']:.2f} | "
            f"{metrics['total_input_tokens']}/"
            f"{metrics['total_output_tokens']} | "
            f"{metrics['avg_duration_ms'] / 1000:.2f}s |"
        )

    lines.extend(
        [
            "",
            "## Attempt evidence",
            "",
            (
                "| Scenario | Rep | Pass | Mutations | Pre-undo verifier "
                "| Restored | Dirty preserved | Repo Map files |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in artifact["rows"]:
        recovery = row["recovery"]
        restored_label = (
            "n/a"
            if row["mode"] == "task_success"
            else "yes"
            if recovery["exact_restoration"]
            else "no"
        )
        dirty_label = (
            "n/a"
            if not row["dirty_paths"]
            else "yes"
            if recovery["dirty_preserved"]
            else "no"
        )
        lines.append(
            f"| `{row['task_id']}` | {row['repetition']} | "
            f"{'yes' if row['passed'] else 'no'} | "
            f"{len(row['mutation_paths'])} | "
            f"{row['pre_undo_verifier']['exit_code']} | "
            f"{restored_label} | "
            f"{dirty_label} | "
            f"{len(row['repo_map_files'])} |"
        )

    lines.extend(
        [
            "",
            "## Protocol",
            "",
            (
                f"- Model: `{artifact['model']}`; temperature 0; "
                f"{artifact['repetitions']} repetitions per scenario."
            ),
            (
                "- `repo_map_cross_module`: a hidden verifier accepts a "
                "cross-module change in a repository containing "
                "legacy/experimental distractors."
            ),
            (
                "- `undo_rejected_multifile_change`: the model is asked to "
                "make a two-file change that a hidden baseline verifier "
                "rejects; Undo must restore the exact pre-run workspace."
            ),
            (
                "- `undo_preserves_preexisting_dirty_file`: the runner "
                "creates a pre-run README edit, asks the model to modify that "
                "same file and source code, then requires Undo to restore the "
                "dirty pre-run bytes rather than the pristine fixture."
            ),
            (
                "- Workspace digests compare every non-runtime file; path "
                "evidence records pristine, pre-run, post-agent, and "
                "post-Undo SHA-256 values."
            ),
            "",
            "## Provenance",
            "",
            f"- Captured at: `{artifact['captured_at']}`",
            f"- Commit: `{runtime['commit_sha']}`",
            f"- Branch: `{runtime['branch']}`",
            f"- Working tree dirty before execution: `{runtime['working_tree_dirty']}`",
            f"- Fixture snapshot: `{artifact['benchmark']['fixture_snapshot_id']}`",
            (
                "- Evaluation snapshot: "
                f"`{artifact['benchmark']['evaluation_snapshot_id']}`"
            ),
            "",
            "## Scope",
            "",
            (
                "This is a nine-attempt engineering regression over three "
                "small scenarios, not a general coding-capability benchmark. "
                "It demonstrates observed task completion and restoration "
                "behavior for this frozen snapshot and model configuration."
            ),
            (
                "Provider transport retries that eventually succeed inside "
                "the model SDK are not emitted as model failures in Pico's "
                "trace, so the artifact does not quantify transient HTTP "
                "retry frequency."
            ),
            "",
        ]
    )
    return "\n".join(lines)


@dataclass
class ReliabilityBenchmarkRunner:
    benchmark_path: Path | str = DEFAULT_RELIABILITY_BENCHMARK_PATH
    artifact_path: Path | str = DEFAULT_RELIABILITY_ARTIFACT_PATH
    report_path: Path | str = DEFAULT_RELIABILITY_REPORT_PATH
    workspace_root: Path | str = DEFAULT_RELIABILITY_WORKSPACE_ROOT
    provider: str = "openai"
    model: str | None = None
    base_url: str | None = None
    repetitions: int = 3
    max_new_tokens: int = 1024
    verifier_timeout: int = 90
    require_clean_worktree: bool = False
    sandbox_config: DockerSandboxConfig | None = None

    def __post_init__(self):
        self.benchmark_path = Path(self.benchmark_path).resolve()
        self.repo_root = self.benchmark_path.parent.parent
        workspace_env = _load_workspace_env(self.repo_root)
        self.artifact_path = Path(self.artifact_path)
        self.report_path = Path(self.report_path)
        self.workspace_root = Path(self.workspace_root)
        if self.provider != "openai":
            raise ValueError("provider must be 'openai'")
        if int(self.repetitions) < 1:
            raise ValueError("repetitions must be positive")
        self.model = str(
            self.model
            or workspace_env.get("OPENAI_MODEL")
            or DEFAULT_OPENAI_MODEL
        ).strip()
        self._shared_model_client = None

    def run(self, task_ids=None):
        benchmark = load_reliability_benchmark(
            self.benchmark_path,
            self.repo_root,
        )
        selected_ids = {str(task_id) for task_id in (task_ids or ())}
        tasks = [
            task
            for task in benchmark["tasks"]
            if not selected_ids or task["id"] in selected_ids
        ]
        unknown_ids = selected_ids - {task["id"] for task in tasks}
        if unknown_ids:
            raise ValueError(
                "unknown reliability task ids: "
                + ", ".join(sorted(unknown_ids))
            )
        self._preflight()
        rows = []
        for repetition in range(1, int(self.repetitions) + 1):
            for task in tasks:
                rows.append(self.run_task(task, repetition=repetition))

        git_status = git_value(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            fallback=None,
            preserve_empty=True,
        )
        artifact = {
            "schema_version": RELIABILITY_ARTIFACT_SCHEMA_VERSION,
            "artifact_type": "pico-reliability-benchmark",
            "execution_mode": "live_llm",
            "captured_at": utc_timestamp(),
            "runtime": {
                "commit_sha": git_value(
                    ["rev-parse", "HEAD"],
                    cwd=self.repo_root,
                ),
                "branch": git_value(
                    ["branch", "--show-current"],
                    cwd=self.repo_root,
                ),
                "working_tree_dirty": (
                    None if git_status is None else bool(git_status)
                ),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
            },
            "benchmark": {
                "name": benchmark.get("name", ""),
                "description": benchmark.get("description", ""),
                "source": str(
                    self.benchmark_path.relative_to(self.repo_root)
                ),
                "task_count": len(tasks),
                "task_ids": [task["id"] for task in tasks],
                "fixture_snapshot_id": _fixture_snapshot_id(
                    tasks,
                    self.repo_root,
                ),
                "evaluation_snapshot_id": _evaluation_snapshot_id(
                    benchmark,
                    tasks,
                    self.repo_root,
                ),
            },
            "provider": self.provider,
            "model": self.model,
            "repetitions": int(self.repetitions),
            "run_config": {
                "temperature": 0.0,
                "max_new_tokens": int(self.max_new_tokens),
                "verifier_timeout_seconds": int(self.verifier_timeout),
                "require_clean_worktree": bool(
                    self.require_clean_worktree
                ),
                "repo_map": "dynamic default",
                "undo_conflict_policy": "fail closed",
            },
            "sandbox": (
                self.sandbox_config or DockerSandboxConfig()
            ).__dict__,
            "summary": summarize_reliability_rows(rows),
            "rows": rows,
        }
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            render_reliability_markdown(artifact),
            encoding="utf-8",
        )
        return artifact

    def run_task(self, task, *, repetition):
        fixture_source = (
            self.repo_root / task["fixture_repo"]
        ).resolve()
        relative_workspace = (
            Path(f"rep-{repetition}")
            / task["id"]
            / fixture_source.name
        )
        workspace_root = (
            self.workspace_root / relative_workspace
        ).resolve()
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        workspace_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, workspace_root)

        pristine_hashes = _workspace_hashes(workspace_root)
        self._apply_preexisting_edits(task, workspace_root)
        pre_run_hashes = _workspace_hashes(workspace_root)
        workspace = WorkspaceContext.build(
            workspace_root,
            repo_root_override=workspace_root,
        )
        sandbox = self._sandbox(workspace_root)
        run_store = RunStore(workspace_root / ".pico" / "runs")
        existing_run_ids = {
            path.name
            for path in run_store.root.glob("run_*")
            if path.is_dir()
        }
        agent = Pico(
            model_client=self._shared_model_client,
            workspace=workspace,
            session_store=SessionStore(
                workspace_root / ".pico" / "sessions"
            ),
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=int(self.max_new_tokens),
            allowed_tools=tuple(task["allowed_tools"]),
            feature_flags=_variant_feature_flags("full"),
            sandbox=sandbox,
        )
        started = time.monotonic()
        final_answer = agent.ask(task["prompt"])
        agent_duration_ms = int((time.monotonic() - started) * 1000)
        task_state = agent.current_task_state
        report = agent.run_store.load_report(task_state.run_id)
        run_dirs = [
            path
            for path in run_store.root.glob("run_*")
            if path.is_dir() and path.name not in existing_run_ids
        ]
        attempt_trace = evidence._attempt_trace_metrics(
            run_store.run_dir(task_state.run_id),
            run_dirs,
            workspace_root,
        )
        trace = attempt_trace["parent"]
        isolation = evidence._workspace_isolation_audit(
            workspace_root,
            run_dirs,
            task,
        )

        after_agent_hashes = _workspace_hashes(workspace_root)
        mutation_paths = _changed_paths(
            pre_run_hashes,
            after_agent_hashes,
        )
        missing_expected_changes = sorted(
            set(task["expected_agent_changes"]) - set(mutation_paths)
        )
        pre_verify_started = time.monotonic()
        if isolation["ok"]:
            pre_undo_verifier = self._verify(
                task,
                workspace_root,
                sandbox,
            )
        else:
            pre_undo_verifier = SandboxResult(
                returncode=125,
                stderr="verifier skipped: workspace isolation audit failed",
            )
        pre_verify_duration_ms = int(
            (time.monotonic() - pre_verify_started) * 1000
        )

        recovery = {
            "attempted": task["mode"] == "undo_recovery",
            "passed": False,
            "dry_run_paths": [],
            "restored_paths": [],
            "deleted_paths": [],
            "already_restored": False,
            "exact_restoration": False,
            "dirty_preserved": not task["dirty_paths"],
            "duration_ms": 0,
            "error": "",
        }
        after_undo_hashes = {}
        post_undo_verifier = SandboxResult(
            returncode=125,
            stderr="not applicable",
        )
        post_verify_duration_ms = 0
        if task["mode"] == "undo_recovery":
            undo_started = time.monotonic()
            try:
                dry_run = restore_run(
                    workspace_root,
                    task_state.run_id,
                    dry_run=True,
                )
                restored = restore_run(
                    workspace_root,
                    task_state.run_id,
                )
                recovery["dry_run_paths"] = list(
                    dry_run.restored_paths
                )
                recovery["restored_paths"] = list(
                    restored.restored_paths
                )
                recovery["deleted_paths"] = list(
                    restored.deleted_paths
                )
                recovery["already_restored"] = bool(
                    restored.already_restored
                )
            except RunUndoError as exc:
                recovery["error"] = str(exc)
            recovery["duration_ms"] = int(
                (time.monotonic() - undo_started) * 1000
            )
            after_undo_hashes = _workspace_hashes(workspace_root)
            recovery["exact_restoration"] = (
                after_undo_hashes == pre_run_hashes
            )
            recovery["dirty_preserved"] = all(
                after_undo_hashes.get(path)
                == pre_run_hashes.get(path)
                and pre_run_hashes.get(path) != pristine_hashes.get(path)
                for path in task["dirty_paths"]
            )
            post_verify_started = time.monotonic()
            post_undo_verifier = self._verify(
                task,
                workspace_root,
                sandbox,
            )
            post_verify_duration_ms = int(
                (time.monotonic() - post_verify_started) * 1000
            )
            recovery["passed"] = (
                not recovery["error"]
                and pre_undo_verifier.returncode != 0
                and post_undo_verifier.returncode == 0
                and recovery["exact_restoration"]
                and recovery["dirty_preserved"]
                and not missing_expected_changes
            )

        trace_parse_errors = list(
            attempt_trace["total"]["trace_parse_errors"]
        )
        if task["mode"] == "task_success":
            passed = (
                task_state.status == "completed"
                and isolation["ok"]
                and pre_undo_verifier.returncode == 0
                and not missing_expected_changes
                and not trace_parse_errors
            )
        else:
            passed = (
                task_state.status == "completed"
                and isolation["ok"]
                and recovery["passed"]
                and not trace_parse_errors
            )
        summary = dict(report.get("summary") or {})
        evidence_paths = (
            set(task["expected_agent_changes"])
            | set(task["dirty_paths"])
            | set(mutation_paths)
            | set(recovery["restored_paths"])
        )
        total_duration_ms = (
            agent_duration_ms
            + pre_verify_duration_ms
            + recovery["duration_ms"]
            + post_verify_duration_ms
        )
        return {
            "task_id": task["id"],
            "category": task["category"],
            "mode": task["mode"],
            "repetition": repetition,
            "passed": passed,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "tool_steps": int(task_state.tool_steps),
            "model_calls": int(
                attempt_trace["total"]["model_calls"]
            ),
            "model_duration_ms": int(
                attempt_trace["total"]["model_duration_ms"]
            ),
            "model_failures": int(
                attempt_trace["total"]["model_failures"]
            ),
            "model_action_rejections": int(
                attempt_trace["total"][
                    "model_action_rejections"
                ]
            ),
            "input_tokens": int(
                attempt_trace["total"]["input_tokens"]
            ),
            "output_tokens": int(
                attempt_trace["total"]["output_tokens"]
            ),
            "cached_tokens": int(
                attempt_trace["total"]["cached_tokens"]
            ),
            "agent_duration_ms": agent_duration_ms,
            "pre_verify_duration_ms": pre_verify_duration_ms,
            "post_verify_duration_ms": post_verify_duration_ms,
            "total_duration_ms": total_duration_ms,
            "executed_tools": list(trace["executed_tools"]),
            "repo_map_files": list(
                summary.get("repo_map_files") or []
            ),
            "reported_changed_files": list(
                summary.get("changed_files") or []
            ),
            "mutation_paths": mutation_paths,
            "expected_agent_changes": list(
                task["expected_agent_changes"]
            ),
            "missing_expected_changes": missing_expected_changes,
            "dirty_paths": list(task["dirty_paths"]),
            "workspace_digests": {
                "pristine": _snapshot_digest(pristine_hashes),
                "pre_run": _snapshot_digest(pre_run_hashes),
                "after_agent": _snapshot_digest(
                    after_agent_hashes
                ),
                "after_undo": (
                    _snapshot_digest(after_undo_hashes)
                    if after_undo_hashes
                    else None
                ),
            },
            "path_hash_evidence": _path_hash_evidence(
                evidence_paths,
                pristine=pristine_hashes,
                pre_run=pre_run_hashes,
                after_agent=after_agent_hashes,
                after_undo=after_undo_hashes,
            ),
            "pre_undo_verifier": self._sandbox_result(
                pre_undo_verifier
            ),
            "post_undo_verifier": self._sandbox_result(
                post_undo_verifier
            ),
            "recovery": recovery,
            "workspace_isolation": isolation,
            "trace_parse_errors": trace_parse_errors,
            "workspace": str(relative_workspace),
            "run_id": task_state.run_id,
            "final_answer": str(final_answer),
        }

    def _preflight(self):
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if self.require_clean_worktree:
            status = git_value(
                ["status", "--porcelain", "--untracked-files=all"],
                cwd=self.repo_root,
                fallback=None,
                preserve_empty=True,
            )
            if status is None:
                raise RuntimeError(
                    "cannot verify that the benchmark worktree is clean"
                )
            if status:
                raise RuntimeError(
                    "benchmark requires a clean git worktree"
                )
        self._shared_model_client = build_real_model_client(
            self.model,
            self.base_url,
            env=_load_workspace_env(self.repo_root),
        )
        DockerSandbox(
            self.workspace_root,
            config=self.sandbox_config or DockerSandboxConfig(),
        ).ensure_ready()

    def _sandbox(self, workspace_root):
        return DockerSandbox(
            workspace_root,
            config=self.sandbox_config or DockerSandboxConfig(),
        )

    def _apply_preexisting_edits(self, task, workspace_root):
        for edit in task["preexisting_edits"]:
            path = workspace_root / edit["path"]
            path.write_text(
                path.read_text(encoding="utf-8") + edit["append"],
                encoding="utf-8",
            )

    def _verify(self, task, workspace_root, sandbox):
        installed = []
        try:
            for verifier_file in task["verifier_files"]:
                source = self.repo_root / verifier_file["source"]
                target = workspace_root / verifier_file["target"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                installed.append(target)
            return sandbox.run(
                task["verifier_command"],
                cwd=workspace_root,
                timeout=int(self.verifier_timeout),
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            for path in installed:
                path.unlink(missing_ok=True)
            for directory in sorted(
                {path.parent for path in installed},
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _sandbox_result(result):
        return {
            "exit_code": int(result.returncode),
            "timed_out": bool(result.timed_out),
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
        }
