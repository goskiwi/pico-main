"""Deterministic native-function Harness regression evaluator."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from pico.contracts import ModelAction
from pico.runtime import Pico, PicoConfig
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext

REQUIRED_TASK_FIELDS = {
    "id", "prompt", "fixture_repo", "allowed_tools", "step_budget",
    "target_path", "old_text", "new_text", "verifier", "category",
}


class BenchmarkModel:
    model = "scripted-native-functions"
    conversation_mode = "responses-manual-replay-v1"
    supports_prompt_cache = False

    def __init__(self, task):
        self.task = task
        self.step = 0
        self.patch_requested = False
        self.last_completion_metadata = {}
        self.reset_action_session()

    def reset_action_session(self):
        self.recorded_action_results = []

    def record_action_result(self, action, result):
        self.recorded_action_results.append((action.kind, str(result)))

    def complete_action(self, prompt, max_new_tokens, **kwargs):
        self.step += 1
        prompt = "\n\n".join(
            [str(prompt), *(result for _kind, result in self.recorded_action_results)]
        )
        behavior = self.task.get("behavior", "")
        if behavior == "path_escape" and self.step == 1:
            return ModelAction.tool("read_file", {"path": "../outside.txt", "start": 1, "end": 20})
        if behavior == "invalid_patch" and self.step == 1:
            return ModelAction.tool(
                "patch_file",
                {"path": self.task["target_path"], "old_text": self.task["old_text"],
                 "new_text": self.task["new_text"], "expected_revision": "absent"},
            )
        read_count = 3 if behavior == "repeated_read" else 1
        offset = 1 if behavior in {"path_escape", "invalid_patch"} else 0
        if self.step <= offset + read_count:
            return ModelAction.tool(
                "read_file", {"path": self.task["target_path"], "start": 1, "end": 200}
            )
        revisions = re.findall(r"revision: (sha256:[a-f0-9]{64})", prompt)
        if revisions and not self.patch_requested:
            self.patch_requested = True
            return ModelAction.tool(
                "patch_file",
                {
                    "path": self.task["target_path"],
                    "old_text": self.task["old_text"],
                    "new_text": self.task["new_text"],
                    "expected_revision": revisions[-1],
                },
            )
        return ModelAction.final("Completed and verified by the fixed Harness.")


def load_benchmark(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"schema_version", "description", "tasks"} or data["schema_version"] != 2:
        raise ValueError("unsupported native Harness benchmark schema")
    ids = set()
    for task in data["tasks"]:
        missing = REQUIRED_TASK_FIELDS - set(task)
        if missing:
            raise ValueError(f"benchmark task missing required fields: {sorted(missing)}")
        if task["id"] in ids or not task["allowed_tools"] or int(task["step_budget"]) < 1:
            raise ValueError("invalid benchmark task identity or budget")
        ids.add(task["id"])
    return data


def _portable_path(path):
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(resolved)


def summarize_rows(rows):
    total = len(rows)
    passed = sum(bool(row["passed"]) for row in rows)
    return {
        "total_tasks": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 0.0,
        "within_budget_rate": sum(bool(row["within_budget"]) for row in rows) / total if total else 0.0,
        "verifier_pass_rate": sum(bool(row["verifier_passed"]) for row in rows) / total if total else 0.0,
        "failure_category_counts": dict(Counter(row.get("failure_category", "") for row in rows if not row["passed"])),
    }


class BenchmarkEvaluator:
    def __init__(self, benchmark_path=Path("benchmarks/coding_tasks.json"),
                 artifact_path=Path("artifacts/harness-regression.json"),
                 workspace_root=Path(".pico/evals")):
        self.benchmark_path = Path(benchmark_path)
        self.artifact_path = Path(artifact_path)
        self.workspace_root = Path(workspace_root)

    def load(self):
        return load_benchmark(self.benchmark_path)

    def run_task(self, task):
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        fixture = Path(tempfile.mkdtemp(prefix=task["id"] + "-", dir=self.workspace_root))
        shutil.copytree(Path(task["fixture_repo"]), fixture, dirs_exist_ok=True)
        agent = Pico(
            model_client=BenchmarkModel(task),
            # Benchmark fixtures deliberately live under a parent workspace by
            # default.  Treat the fresh copy as the repository root so Git
            # discovery cannot redirect tool mutations to the outer checkout.
            workspace=WorkspaceContext.build(fixture, repo_root_override=fixture),
            session_store=SessionStore(fixture / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="auto",
                max_tool_executions=int(task["step_budget"]),
                max_new_tokens=128,
                allowed_tools=task["allowed_tools"],
                verification_command="",
            ),
        )
        answer = agent.ask(task["prompt"])
        argv = shlex.split(task["verifier"])
        verified = subprocess.run(
            argv, cwd=fixture, capture_output=True, text=True, timeout=20, check=False
        ).returncode == 0
        state = agent.run.task_state
        within_budget = state.executed_tool_count <= int(task["step_budget"])
        passed = state.status == "completed" and within_budget and verified
        run_dir = agent.dependencies.run_store.run_dir(state.run_id)
        return {
            "id": task["id"], "category": task["category"], "status": "pass" if passed else "fail",
            "passed": passed, "within_budget": within_budget, "verifier_passed": verified,
            "executed_tool_count": state.executed_tool_count, "stop_reason": state.stop_reason, "answer": answer,
            "fixture_copy": _portable_path(fixture), "run_dir": _portable_path(run_dir),
            "failure_category": "" if passed else ("verifier_failed" if not verified else "runtime_failed"),
        }

    def run(self):
        benchmark = self.load()
        rows = [self.run_task(task) for task in benchmark["tasks"]]
        artifact = {
            "artifact_type": "harness-regression",
            "summary": summarize_rows(rows),
            "rows": rows,
        }
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return artifact


def run_harness_regression(**kwargs):
    return BenchmarkEvaluator(**kwargs).run()
