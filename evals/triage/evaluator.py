"""Deterministic end-to-end Pico Triage evaluation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from applications.triage import TriageCase, TriageWorkflow
from pico import ModelAction, PicoConfig
from pico.sandbox import SandboxResult

from .metrics import summarize_triage_rows

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASE_ROOT = Path(__file__).resolve().parent / "cases"


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    fixture_repo: str
    failing_command: str
    ci_log: str
    target_path: str
    old_text: str
    new_text: str
    expected_root_file: str
    tool_budget: int = Field(default=4, ge=1)


class HostEvaluationSandbox:
    """Execute fixed evaluator commands without weakening Pico production policy."""

    def run(self, argv, *, cwd, timeout, env=None, execution_context=None):
        if execution_context is not None:
            timeout = execution_context.bounded_timeout(timeout)
        try:
            result = subprocess.run(
                list(argv),
                cwd=cwd,
                env={**os.environ, **dict(env or {})},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return SandboxResult(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                returncode=None,
                stdout=str(exc.stdout or ""),
                stderr=str(exc.stderr or ""),
                timed_out=True,
                stop_reason="deadline_exceeded",
            )


class ScriptedTriageModel:
    model = "scripted-triage"
    supports_prompt_cache = False

    def __init__(self, case: EvalCase):
        self.case = case
        self.step = 0
        self.last_completion_metadata = {}
        self.reset_action_session()

    @staticmethod
    def estimate_action_tool_tokens(_action_tools, _token_counter):
        return 0

    def reset_action_session(self):
        self.recorded_action_results = []

    def record_action_result(self, action, result):
        self.recorded_action_results.append((action.kind, str(result)))

    def complete_action(self, _prompt, _max_new_tokens, **_kwargs):
        self.step += 1
        if self.step == 1:
            return ModelAction.tool(
                "run_shell",
                {"command": self.case.failing_command, "timeout": 20},
                call_id="call_reproduce",
            )
        if self.step == 2:
            return ModelAction.tool(
                "read_file",
                {"path": self.case.target_path, "start": 1, "end": 200},
                call_id="call_source",
            )
        if self.step == 3:
            transcript = "\n".join(result for _kind, result in self.recorded_action_results)
            revisions = re.findall(r"revision: (sha256:[a-f0-9]{64})", transcript)
            if not revisions:
                return ModelAction.invalid("Read the target before patching it.")
            return ModelAction.tool(
                "patch_file",
                {
                    "path": self.case.target_path,
                    "old_text": self.case.old_text,
                    "new_text": self.case.new_text,
                    "expected_revision": revisions[-1],
                },
                call_id="call_patch",
            )
        diagnosis = {
            "status": "fixed",
            "root_cause": {
                "summary": "The failing assertion is caused by the stale target value.",
                "files": [self.case.expected_root_file],
            },
            "evidence": [
                {
                    "kind": "test_result",
                    "claim": "The configured CI command reproduced the failure.",
                    "tool_call_id": "call_reproduce",
                    "path": "",
                    "line": None,
                },
                {
                    "kind": "source",
                    "claim": "The target file contained the stale value.",
                    "tool_call_id": "call_source",
                    "path": self.case.expected_root_file,
                    "line": 1,
                },
            ],
        }
        return ModelAction.final(json.dumps(diagnosis, ensure_ascii=False))


def load_cases(case_root=DEFAULT_CASE_ROOT):
    return [
        EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(Path(case_root).glob("*.json"))
    ]


def run_triage_evaluation(
    path=Path("artifacts/triage-evaluation.json"),
    *,
    case_root=DEFAULT_CASE_ROOT,
):
    rows = []
    with tempfile.TemporaryDirectory(prefix="pico-triage-eval-") as directory:
        root = Path(directory)
        for case in load_cases(case_root):
            repository = root / case.incident_id
            shutil.copytree(ROOT / case.fixture_repo, repository)
            triage_case = TriageCase(
                incident_id=case.incident_id,
                repository_root=repository,
                failing_command=case.failing_command,
                verification_command=case.failing_command,
                ci_log=case.ci_log,
                constraints=(f"Only modify {case.target_path}",),
            )
            report = TriageWorkflow(
                ScriptedTriageModel(case),
                config=PicoConfig(
                    approval_policy="auto",
                    max_tool_executions=case.tool_budget,
                ),
                sandbox=HostEvaluationSandbox(),
            ).run(triage_case)
            target = repository / case.target_path
            rows.append(
                {
                    "incident_id": case.incident_id,
                    "reproduced": report.reproduction.status == "reproduced",
                    "root_cause_top1": (
                        report.root_cause.files[0] == case.expected_root_file
                    ),
                    "patch_correct": case.new_text in target.read_text(encoding="utf-8"),
                    "verification_passed": report.verification.status == "passed",
                    "within_budget": report.executed_tool_count <= case.tool_budget,
                    "changed_paths": list(report.patch.changed_paths),
                }
            )
    payload = {
        "artifact_type": "triage-evaluation",
        "rows": rows,
        "summary": summarize_triage_rows(rows),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
