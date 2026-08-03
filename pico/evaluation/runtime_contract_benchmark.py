"""Deterministic, paired runtime-contract benchmark for Pico.

This module deliberately evaluates runtime behavior with scripted actions and
controlled workspace perturbations.  It is separate from the live-model
benchmark: passing it demonstrates a reproducible runtime contract, not a
model's general coding ability.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from pico.agent.actions import ModelAction
from pico.agent.checkpoints import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_SCHEMA_MISMATCH_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from pico.context_manager import ContextManager
from pico.context_types import count_tokens, tokenizer_details
from pico.runtime import Pico
from pico.sandbox import SandboxResult
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext

from .common import git_value, safe_ratio, utc_timestamp


RUNTIME_CONTRACT_BENCHMARK_SCHEMA_VERSION = 1
RUNTIME_CONTRACT_ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_RUNTIME_CONTRACT_BENCHMARK_PATH = Path(
    "benchmarks/runtime_contract_tasks_v1.json"
)
DEFAULT_RUNTIME_CONTRACT_ARTIFACT_PATH = Path(
    "artifacts/runtime-contract-benchmark-v1-3x.json"
)
DEFAULT_RUNTIME_CONTRACT_REPORT_PATH = Path(
    "artifacts/runtime-contract-benchmark-v1-3x.md"
)
DEFAULT_RUNTIME_CONTRACT_WORKSPACE_ROOT = Path(
    "artifacts/runtime-contract-workspaces"
)
SUPPORTED_HANDLERS = frozenset(
    {
        "context_budget",
        "memory_dedup",
        "resume_validation",
        "partial_success",
    }
)
REQUIRED_TASK_KEYS = (
    "id",
    "family",
    "handler",
    "control",
    "candidate",
    "acceptance",
)


def _digest(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


_VOLATILE_OUTCOME_KEYS = frozenset(
    {
        "captured_at",
        "created_at",
        "session_id",
        "selected_session_id",
        "main_session_id",
        "delegate_session_id",
        "expected_session_id",
    }
)
_SESSION_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]+$")


def _stable_outcome(value, *, key=""):
    """Remove generated identifiers and timestamps from repeatability hashes."""
    if key in _VOLATILE_OUTCOME_KEYS:
        return "<volatile>"
    if isinstance(value, dict):
        return {
            str(item_key): _stable_outcome(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_stable_outcome(item) for item in value]
    if isinstance(value, str) and _SESSION_ID_PATTERN.fullmatch(value):
        return "<session-id>"
    return value


def _file_digest(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _relative(path, root):
    return str(Path(path).resolve().relative_to(Path(root).resolve()))


def _resolve_from_root(path, root):
    path = Path(path)
    return path if path.is_absolute() else Path(root) / path


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as handle:
        handle.write(text)
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def validate_runtime_contract_benchmark(payload, repo_root):
    """Validate and normalize the frozen deterministic benchmark manifest."""
    if not isinstance(payload, dict):
        raise ValueError("runtime contract benchmark must be an object")
    if (
        int(payload.get("schema_version", 0))
        != RUNTIME_CONTRACT_BENCHMARK_SCHEMA_VERSION
    ):
        raise ValueError("unsupported runtime contract benchmark schema_version")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("runtime contract benchmark name must be non-empty")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("runtime contract benchmark tasks must be a non-empty list")

    root = Path(repo_root).resolve()
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
        handler = str(task["handler"]).strip()
        if handler not in SUPPORTED_HANDLERS:
            raise ValueError(
                f"task {task_id!r} handler must be one of: "
                + ", ".join(sorted(SUPPORTED_HANDLERS))
            )
        acceptance = task["acceptance"]
        if not isinstance(acceptance, list) or not acceptance:
            raise ValueError(f"task {task_id!r} acceptance must be a non-empty list")
        normalized_acceptance = [str(item).strip() for item in acceptance]
        if any(not item for item in normalized_acceptance):
            raise ValueError(
                f"task {task_id!r} acceptance must contain non-empty strings"
            )
        task.update(
            {
                "id": task_id,
                "family": str(task["family"]).strip(),
                "handler": handler,
                "control": str(task["control"]).strip(),
                "candidate": str(task["candidate"]).strip(),
                "acceptance": normalized_acceptance,
            }
        )
        if not all(task[key] for key in ("family", "control", "candidate")):
            raise ValueError(
                f"task {task_id!r} family, control, and candidate must be non-empty"
            )
        normalized_tasks.append(task)

    normalized = dict(payload)
    normalized["name"] = name
    normalized["tasks"] = normalized_tasks
    try:
        Path(root).relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive type boundary
        raise ValueError("invalid repository root") from exc
    return normalized


def load_runtime_contract_benchmark(
    path=DEFAULT_RUNTIME_CONTRACT_BENCHMARK_PATH,
    repo_root=None,
):
    path = Path(path)
    root = Path(repo_root or path.parent.parent).resolve()
    return validate_runtime_contract_benchmark(
        json.loads(path.read_text(encoding="utf-8")),
        root,
    )


class ScriptedContractModelClient:
    """A local action source used only by the deterministic benchmark."""

    model = "runtime-contract-scripted"
    base_url = "test://runtime-contract"
    supports_prompt_cache = False

    def __init__(self, actions):
        self.actions = list(actions)
        self.prompts = []
        self.recorded_results = []
        self.last_completion_metadata = {}

    def complete_action(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(str(prompt))
        if not self.actions:
            raise RuntimeError("runtime contract action sequence was exhausted")
        return self.actions.pop(0)

    def reset_action_session(self):
        return None

    def count_tokens(self, text):
        return count_tokens(text, model=self.model)

    def tokenizer_metadata(self):
        return tokenizer_details(self.model)

    def record_action_result(self, action, result):
        self.recorded_results.append((action.name, str(result)))


class ContractSandbox:
    """A non-executing sandbox double for scenarios that do not run shell."""

    backend = "runtime-contract"

    def __init__(self, workspace_root):
        self.workspace_root = Path(workspace_root)
        self.config = SimpleNamespace(image="pico/runtime-contract:scripted")

    def identity(self):
        return {
            "backend": self.backend,
            "image": self.config.image,
            "cpus": 1.0,
            "memory": "128m",
            "pids_limit": 32,
            "network": "disabled-by-contract",
            "rootfs_read_only": False,
        }

    def audit_metadata(self, *, timed_out=False):
        return {
            "sandbox_backend": self.backend,
            "sandbox_image": self.config.image,
            "sandbox_network": "disabled-by-contract",
            "sandbox_rootfs_read_only": False,
            "sandbox_cpus": 1.0,
            "sandbox_memory": "128m",
            "sandbox_pids_limit": 32,
            "sandbox_timed_out": bool(timed_out),
        }

    def run(self, command, *, cwd, timeout, env=None):
        del command, cwd, timeout, env
        raise AssertionError("this runtime-contract scenario must not execute shell")


class FailingContractSandbox(ContractSandbox):
    """Returns a non-zero result and optionally creates one workspace file."""

    def __init__(self, workspace_root, *, mutate_workspace):
        super().__init__(workspace_root)
        self.mutate_workspace = bool(mutate_workspace)

    def run(self, command, *, cwd, timeout, env=None):
        del command, timeout, env
        if self.mutate_workspace:
            Path(cwd, "changed-by-command.txt").write_text(
                "partial result\n",
                encoding="utf-8",
            )
        return SandboxResult(
            returncode=1,
            stdout="created partial result" if self.mutate_workspace else "no output",
            stderr="command failed",
        )


def _tool_action(name, args, call_id):
    return ModelAction.tool(
        name,
        args,
        protocol="runtime_contract_v1",
        call_id=call_id,
    )


def _final_action(answer, call_id):
    return ModelAction.final(
        answer,
        protocol="runtime_contract_v1",
        call_id=call_id,
    )


def _workspace(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text("runtime contract fixture\n", encoding="utf-8")
    return WorkspaceContext.build(root, repo_root_override=root)


def _agent(root, actions=(), *, sandbox=None, session_store=None, **kwargs):
    root = Path(root)
    workspace = _workspace(root)
    store = session_store or SessionStore(root / ".pico" / "sessions")
    flags = {"repo_map": False}
    flags.update(kwargs.pop("feature_flags", {}) or {})
    return Pico(
        model_client=ScriptedContractModelClient(actions),
        workspace=workspace,
        session_store=store,
        sandbox=sandbox or ContractSandbox(root),
        approval_policy=kwargs.pop("approval_policy", "auto"),
        feature_flags=flags,
        **kwargs,
    )


def _check(name, passed, *, expected=None, actual=None):
    result = {"name": str(name), "passed": bool(passed)}
    if expected is not None:
        result["expected"] = expected
    if actual is not None:
        result["actual"] = actual
    return result


def _verification(name, checks):
    return {
        "name": name,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def _context_fixture(root, *, context_reduction):
    agent = _agent(
        root,
        feature_flags={
            "context_reduction": context_reduction,
            "dynamic_budget": False,
        },
    )
    agent.prefix = "PREFIX_HISTORY " + ("old baseline detail " * 300)
    for index in range(4):
        agent.memory.append_note(
            f"MEMORY_HISTORY_{index} " + ("previous investigation detail " * 100),
            source="runtime-contract",
        )
    manager = ContextManager(
        agent,
        total_budget=220,
        section_budgets={
            "prefix": 180,
            "memory": 180,
            "skills": 0,
            "repo_map": 0,
            "task_context": 80,
        },
        section_floors={
            "prefix": 0,
            "memory": 0,
            "skills": 0,
            "repo_map": 0,
            "task_context": 0,
        },
    )
    request = (
        "CURRENT_REQUEST_BEGIN update target.py with the verified fix "
        "and run its focused test CURRENT_REQUEST_END"
    )
    prompt, metadata = manager.build(request)
    return {
        "prompt_tokens": agent.count_tokens(prompt),
        "budget_tokens": 220,
        "over_budget": agent.count_tokens(prompt) > 220,
        "current_request_intact": f"Current user request:\n{request}" in prompt,
        "current_request_truncated": bool(
            metadata["current_request"]["truncated"]
        ),
        "budget_reduction_count": len(metadata["budget_reductions"]),
        "reduction_sections": sorted(
            {entry["section"] for entry in metadata["budget_reductions"]}
        ),
        "prefix_raw_tokens": metadata["sections"]["prefix"]["raw_tokens"],
        "prefix_rendered_tokens": metadata["sections"]["prefix"][
            "rendered_tokens"
        ],
        "memory_raw_tokens": metadata["sections"]["memory"]["raw_tokens"],
        "memory_rendered_tokens": metadata["sections"]["memory"][
            "rendered_tokens"
        ],
    }


def _run_context_budget(root, task):
    control = _context_fixture(root / "control", context_reduction=False)
    candidate = _context_fixture(root / "candidate", context_reduction=True)
    checks = [
        _check(
            "control_is_over_budget",
            control["over_budget"],
            expected=True,
            actual=control["over_budget"],
        ),
        _check(
            "candidate_within_budget",
            candidate["prompt_tokens"] <= candidate["budget_tokens"],
            expected=f"<= {candidate['budget_tokens']}",
            actual=candidate["prompt_tokens"],
        ),
        _check(
            "candidate_preserves_complete_current_request",
            candidate["current_request_intact"]
            and not candidate["current_request_truncated"],
            expected=True,
            actual={
                "intact": candidate["current_request_intact"],
                "truncated": candidate["current_request_truncated"],
            },
        ),
        _check(
            "candidate_reduces_old_context",
            candidate["budget_reduction_count"] > 0
            and candidate["prefix_rendered_tokens"]
            < candidate["prefix_raw_tokens"],
            expected=True,
            actual={
                "reduction_count": candidate["budget_reduction_count"],
                "prefix_tokens": [
                    candidate["prefix_raw_tokens"],
                    candidate["prefix_rendered_tokens"],
                ],
            },
        ),
    ]
    return {
        "control": {"name": task["control"], "metrics": control},
        "candidate": {"name": task["candidate"], "metrics": candidate},
        "verifier": _verification("context_budget_contract_v1", checks),
    }


def _memory_sequence(root, *, read_only_dedup):
    root = Path(root)
    (root / "module.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "module.py").write_text("answer = 42\n", encoding="utf-8")
    read = _tool_action(
        "read_file",
        {"files": [{"path": "module.py", "start": 1, "end": 1}]},
        "read",
    )
    agent = _agent(
        root,
        [
            read,
            _tool_action(
                "read_file",
                {"files": [{"path": "module.py", "start": 1, "end": 1}]},
                "duplicate-read",
            ),
            _tool_action(
                "write_file",
                {"path": "module.py", "content": "after = 2\n"},
                "write",
            ),
            _tool_action(
                "read_file",
                {"files": [{"path": "module.py", "start": 1, "end": 1}]},
                "read-after-write",
            ),
            _final_action("Used current file evidence.", "final"),
        ],
        feature_flags={"read_only_dedup": read_only_dedup},
        max_steps=5,
    )
    physical_reads = []
    original_read = agent.tools["read_file"]["run"]

    def count_physical_reads(args):
        result = original_read(args)
        physical_reads.append(result)
        return result

    agent.tools["read_file"]["run"] = count_physical_reads
    answer = agent.ask("Read the same file, update it, then inspect it again.")
    statuses = [entry["status"] for entry in agent.tool_audit_log]
    errors = [entry["error_code"] for entry in agent.tool_audit_log]
    recorded = [result for name, result in agent.model_client.recorded_results if name == "read_file"]
    return {
        "answer": answer,
        "physical_read_calls": len(physical_reads),
        "tool_statuses": statuses,
        "duplicate_rejections": errors.count("duplicate_read_only_call"),
        "second_read_contains_initial_evidence": len(recorded) >= 2
        and "answer = 42" in recorded[1],
        "changed_file_re_read": bool(physical_reads)
        and "after = 2" in physical_reads[-1],
        "fresh_summary": agent.memory.to_dict()["file_summaries"].get("module.py", {}),
    }


def _run_memory_dedup(root, task):
    control = _memory_sequence(root / "control", read_only_dedup=False)
    candidate = _memory_sequence(root / "candidate", read_only_dedup=True)
    checks = [
        _check(
            "control_performs_every_scripted_read",
            control["physical_read_calls"] == 3,
            expected=3,
            actual=control["physical_read_calls"],
        ),
        _check(
            "candidate_blocks_unchanged_duplicate_read",
            candidate["physical_read_calls"] == 2
            and candidate["duplicate_rejections"] == 1,
            expected={"physical_read_calls": 2, "duplicate_rejections": 1},
            actual={
                "physical_read_calls": candidate["physical_read_calls"],
                "duplicate_rejections": candidate["duplicate_rejections"],
            },
        ),
        _check(
            "candidate_replays_cached_evidence",
            candidate["second_read_contains_initial_evidence"],
            expected=True,
            actual=candidate["second_read_contains_initial_evidence"],
        ),
        _check(
            "candidate_reallows_read_after_workspace_change",
            candidate["changed_file_re_read"]
            and "after = 2" in candidate["fresh_summary"].get("summary", ""),
            expected=True,
            actual={
                "changed_file_re_read": candidate["changed_file_re_read"],
                "summary": candidate["fresh_summary"].get("summary", ""),
            },
        ),
    ]
    return {
        "control": {"name": task["control"], "metrics": control},
        "candidate": {"name": task["candidate"], "metrics": candidate},
        "verifier": _verification("memory_dedup_contract_v1", checks),
    }


def _checkpointed_agent(root):
    root = Path(root)
    (root / "tracked.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    agent = _agent(
        root,
        [
            _tool_action(
                "read_file",
                {"files": [{"path": "tracked.py", "start": 1, "end": 1}]},
                "read-tracked",
            ),
            _final_action("Initial inspection complete.", "initial-final"),
        ],
    )
    answer = agent.ask("Inspect tracked.py before resuming.")
    checkpoint_id = agent.session["checkpoints"]["current_id"]
    return agent, answer, checkpoint_id


def _resume_agent(agent, root, actions, **kwargs):
    root = Path(root)
    feature_flags = {"repo_map": False}
    feature_flags.update(kwargs.pop("feature_flags", {}) or {})
    return Pico.from_session(
        model_client=ScriptedContractModelClient(actions),
        workspace=_workspace(root),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        sandbox=ContractSandbox(root),
        approval_policy=kwargs.pop("approval_policy", "auto"),
        feature_flags=feature_flags,
        **kwargs,
    )


def _resume_probe(root, mode):
    root = Path(root)
    agent, initial_answer, checkpoint_id = _checkpointed_agent(root)
    expected_status = CHECKPOINT_FULL_VALID_STATUS
    if mode == "file_stale":
        (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        expected_status = CHECKPOINT_PARTIAL_STALE_STATUS
    elif mode == "runtime_drift":
        expected_status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
    elif mode == "schema_mismatch":
        saved = agent.session_store.load(agent.session["id"])
        saved["checkpoints"]["items"][checkpoint_id]["schema_version"] = "legacy-v0"
        agent.session_store.save(saved)
        expected_status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
    elif mode != "valid":
        raise ValueError(f"unsupported resume probe: {mode}")

    resume_kwargs = {}
    if mode == "runtime_drift":
        resume_kwargs.update({"approval_policy": "never", "max_steps": 7})
    resumed = _resume_agent(
        agent,
        root,
        [_final_action("Resume completed.", "resume-final")],
        **resume_kwargs,
    )
    status_before_ask = resumed.resume_state["status"]
    answer = resumed.ask("Continue the previous task.")
    prompt = resumed.model_client.prompts[0] if resumed.model_client.prompts else ""
    return {
        "status": status_before_ask,
        "expected_status": expected_status,
        "answer": answer,
        "stale_paths": list(resumed.resume_state["stale_paths"]),
        "mismatch_fields": list(
            resumed.resume_state["runtime_identity_mismatch_fields"]
        ),
        "file_summary_present": "tracked.py"
        in resumed.memory.to_dict()["file_summaries"],
        "checkpoint_rendered": "Task checkpoint:" in prompt,
        "prompt_resume_status": resumed.last_prompt_metadata.get("resume_status"),
    }


def _latest_main_session_probe(root):
    root = Path(root)
    workspace = _workspace(root)
    store = SessionStore(root / ".pico" / "sessions")
    main = _agent(root, session_store=store)
    delegate = _agent(
        root,
        session_store=store,
        agent_mode="delegate",
        parent_agent_id=main.agent_id,
    )
    os.utime(main.session_path, (1, 1))
    os.utime(delegate.session_path, (2, 2))
    del workspace
    return {
        "selected_session_id": store.latest(),
        "main_session_id": main.session["id"],
        "delegate_session_id": delegate.session["id"],
    }


def _corrupt_session_probe(root):
    root = Path(root)
    agent = _agent(root)
    corrupt = agent.session_store.root / "corrupt.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    newer_than_main = time.time() + 10
    os.utime(corrupt, (newer_than_main, newer_than_main))
    return {
        "selected_session_id": agent.session_store.latest(),
        "expected_session_id": agent.session["id"],
    }


def _run_resume_validation(root, task):
    valid = _resume_probe(root / "valid", "valid")
    file_stale = _resume_probe(root / "file-stale", "file_stale")
    runtime_drift = _resume_probe(root / "runtime-drift", "runtime_drift")
    schema_mismatch = _resume_probe(root / "schema-mismatch", "schema_mismatch")
    latest = _latest_main_session_probe(root / "latest-main")
    corrupt = _corrupt_session_probe(root / "corrupt-session")
    checks = [
        _check(
            "valid_checkpoint_resumes",
            valid["status"] == CHECKPOINT_FULL_VALID_STATUS
            and valid["checkpoint_rendered"],
            expected=CHECKPOINT_FULL_VALID_STATUS,
            actual=valid["status"],
        ),
        _check(
            "changed_file_invalidates_summary",
            file_stale["status"] == CHECKPOINT_PARTIAL_STALE_STATUS
            and file_stale["stale_paths"] == ["tracked.py"]
            and not file_stale["file_summary_present"],
            expected={
                "status": CHECKPOINT_PARTIAL_STALE_STATUS,
                "stale_paths": ["tracked.py"],
                "file_summary_present": False,
            },
            actual={
                "status": file_stale["status"],
                "stale_paths": file_stale["stale_paths"],
                "file_summary_present": file_stale["file_summary_present"],
            },
        ),
        _check(
            "runtime_identity_mismatch_is_detected",
            runtime_drift["status"] == CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            and runtime_drift["mismatch_fields"] == ["approval_policy", "max_steps"],
            expected={
                "status": CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
                "mismatch_fields": ["approval_policy", "max_steps"],
            },
            actual={
                "status": runtime_drift["status"],
                "mismatch_fields": runtime_drift["mismatch_fields"],
            },
        ),
        _check(
            "checkpoint_schema_mismatch_is_detected",
            schema_mismatch["status"] == CHECKPOINT_SCHEMA_MISMATCH_STATUS,
            expected=CHECKPOINT_SCHEMA_MISMATCH_STATUS,
            actual=schema_mismatch["status"],
        ),
        _check(
            "latest_ignores_delegate_session",
            latest["selected_session_id"] == latest["main_session_id"],
            expected=latest["main_session_id"],
            actual=latest["selected_session_id"],
        ),
        _check(
            "latest_skips_corrupt_session",
            corrupt["selected_session_id"] == corrupt["expected_session_id"],
            expected=corrupt["expected_session_id"],
            actual=corrupt["selected_session_id"],
        ),
    ]
    control = {
        "name": task["control"],
        "metrics": {
            "status": valid["status"],
            "checkpoint_rendered": valid["checkpoint_rendered"],
            "prompt_resume_status": valid["prompt_resume_status"],
        },
    }
    candidate = {
        "name": task["candidate"],
        "metrics": {
            "file_freshness": file_stale,
            "runtime_identity": runtime_drift,
            "schema": schema_mismatch,
            "latest_main_session": latest,
            "corrupt_session": corrupt,
        },
    }
    return {
        "control": control,
        "candidate": candidate,
        "verifier": _verification("resume_validation_contract_v1", checks),
    }


def _failing_tool_probe(root, *, mutate_workspace):
    root = Path(root)
    agent = _agent(
        root,
        approval_policy="auto",
        sandbox=FailingContractSandbox(
            root,
            mutate_workspace=mutate_workspace,
        ),
    )
    result = agent.run_tool(
        "run_shell",
        {"command": "python -m compileall -q .", "timeout": 20},
    )
    metadata = dict(agent._last_tool_result_metadata)
    return {
        "result_contains_exit_code": "exit_code: 1" in result,
        "tool_status": metadata.get("tool_status"),
        "tool_error_code": metadata.get("tool_error_code"),
        "workspace_changed": metadata.get("workspace_changed"),
        "affected_paths": metadata.get("affected_paths"),
        "diff_summary": metadata.get("diff_summary"),
        "process_note_recorded": any(
            "run_shell partial_success on changed-by-command.txt" in note["text"]
            for note in agent.memory.to_dict()["process_notes"]
        ),
    }


def _run_partial_success(root, task):
    control = _failing_tool_probe(root / "control", mutate_workspace=False)
    candidate = _failing_tool_probe(root / "candidate", mutate_workspace=True)
    checks = [
        _check(
            "non_mutating_nonzero_exit_is_error",
            control["tool_status"] == "error" and not control["workspace_changed"],
            expected={"tool_status": "error", "workspace_changed": False},
            actual={
                "tool_status": control["tool_status"],
                "workspace_changed": control["workspace_changed"],
            },
        ),
        _check(
            "mutating_nonzero_exit_is_partial_success",
            candidate["tool_status"] == "partial_success"
            and candidate["workspace_changed"] is True,
            expected={"tool_status": "partial_success", "workspace_changed": True},
            actual={
                "tool_status": candidate["tool_status"],
                "workspace_changed": candidate["workspace_changed"],
            },
        ),
        _check(
            "partial_success_retains_diff_evidence",
            candidate["affected_paths"] == ["changed-by-command.txt"]
            and candidate["diff_summary"] == ["created:changed-by-command.txt"]
            and candidate["process_note_recorded"],
            expected={
                "affected_paths": ["changed-by-command.txt"],
                "diff_summary": ["created:changed-by-command.txt"],
                "process_note_recorded": True,
            },
            actual={
                "affected_paths": candidate["affected_paths"],
                "diff_summary": candidate["diff_summary"],
                "process_note_recorded": candidate["process_note_recorded"],
            },
        ),
    ]
    return {
        "control": {"name": task["control"], "metrics": control},
        "candidate": {"name": task["candidate"], "metrics": candidate},
        "verifier": _verification("partial_success_contract_v1", checks),
    }


HANDLERS = {
    "context_budget": _run_context_budget,
    "memory_dedup": _run_memory_dedup,
    "resume_validation": _run_resume_validation,
    "partial_success": _run_partial_success,
}


def summarize_runtime_contract_rows(rows):
    rows = list(rows)
    task_rows = {}
    family_rows = {}
    for row in rows:
        task_rows.setdefault(row["task_id"], []).append(row)
        family_rows.setdefault(row["family"], []).append(row)

    def _summary_for(selected):
        passed = sum(bool(row["passed"]) for row in selected)
        return {
            "attempt_count": len(selected),
            "passed": passed,
            "pass_rate": safe_ratio(passed, len(selected)),
            "unique_outcome_fingerprints": len(
                {row["outcome_fingerprint"] for row in selected}
            ),
        }

    return {
        **_summary_for(rows),
        "tasks": {
            task_id: _summary_for(selected)
            for task_id, selected in sorted(task_rows.items())
        },
        "families": {
            family: _summary_for(selected)
            for family, selected in sorted(family_rows.items())
        },
    }


def render_runtime_contract_markdown(artifact):
    summary = artifact["summary"]
    runtime = artifact["runtime"]
    benchmark = artifact["benchmark"]
    lines = [
        "# Pico runtime-contract benchmark V1",
        "",
        "## Result",
        "",
        (
            f"- Overall: **{summary['passed']}/{summary['attempt_count']} "
            f"({summary['pass_rate']:.1%})** isolated deterministic attempts "
            "satisfied every pre-registered verifier."
        ),
        (
            "- Repetitions: "
            f"**{artifact['repetitions']}** fresh workspace(s) per task; "
            "each task's normalized outcome fingerprint is listed below."
        ),
        "- Remote model calls: **0**. The action source is a versioned scripted client.",
        "",
        "## Per-task repeatability",
        "",
        "| Task | Family | Passed | Unique normalized outcome fingerprints |",
        "|---|---|---:|---:|",
    ]
    for task_id, metrics in summary["tasks"].items():
        family = next(
            row["family"] for row in artifact["rows"] if row["task_id"] == task_id
        )
        lines.append(
            f"| `{task_id}` | {family} | "
            f"{metrics['passed']}/{metrics['attempt_count']} | "
            f"{metrics['unique_outcome_fingerprints']} |"
        )

    lines.extend(
        [
            "",
            "## Paired observations (first repetition)",
            "",
        ]
    )
    first_rows = {}
    for row in artifact["rows"]:
        first_rows.setdefault(row["task_id"], row)
    context = first_rows.get("ctx_budget_preserves_current_request")
    if context:
        control = context["control"]["metrics"]
        candidate = context["candidate"]["metrics"]
        lines.append(
            "- Context budget: control rendered "
            f"{control['prompt_tokens']} tokens for a {control['budget_tokens']}-token "
            "budget; candidate rendered "
            f"{candidate['prompt_tokens']} tokens, retained the full current request, "
            f"and recorded {candidate['budget_reduction_count']} old-context reduction(s)."
        )
    memory = first_rows.get("memory_deduplicates_unchanged_read")
    if memory:
        control = memory["control"]["metrics"]
        candidate = memory["candidate"]["metrics"]
        lines.append(
            "- Memory/read guard: the same scripted sequence performed "
            f"{control['physical_read_calls']} physical reads in the control and "
            f"{candidate['physical_read_calls']} in the candidate; the candidate "
            "replayed cached evidence once and re-read after the workspace write."
        )
    resume = first_rows.get("resume_validates_checkpoint_freshness")
    if resume:
        metrics = resume["candidate"]["metrics"]
        lines.append(
            "- Resume validation: controlled perturbations produced "
            f"`{metrics['file_freshness']['status']}`, "
            f"`{metrics['runtime_identity']['status']}`, and "
            f"`{metrics['schema']['status']}` respectively; latest-session "
            "selection also rejected delegate and corrupt entries."
        )
    partial = first_rows.get("tool_classifies_mutating_failure")
    if partial:
        control = partial["control"]["metrics"]
        candidate = partial["candidate"]["metrics"]
        lines.append(
            "- Tool outcome: a non-mutating failure was classified "
            f"`{control['tool_status']}`; the same non-zero exit with a workspace "
            f"write was classified `{candidate['tool_status']}` with preserved diff evidence."
        )

    lines.extend(
        [
            "",
            "## Attempt evidence",
            "",
            "| Task | Rep | Control | Candidate | Verifier | Outcome fingerprint |",
            "|---|---:|---|---|---:|---|",
        ]
    )
    for row in artifact["rows"]:
        lines.append(
            f"| `{row['task_id']}` | {row['repetition']} | "
            f"{row['control']['name']} | {row['candidate']['name']} | "
            f"{'PASS' if row['passed'] else 'FAIL'} | "
            f"`{row['outcome_fingerprint']}` |"
        )

    lines.extend(
        [
            "",
            "## Protocol",
            "",
            "- Every attempt starts from a new, local workspace directory.",
            "- Each task compares a fixed control with one runtime treatment or controlled perturbation.",
            "- The row verifier records individual expected/actual checks in the JSON artifact.",
            "- Outcome fingerprints normalize generated timestamps and session identifiers before comparison.",
            "- The benchmark has no provider, network, or live-model dependency.",
            "",
            "Reproduce from the repository root:",
            "",
            "```bash",
            "uv run python scripts/run_runtime_contract_benchmark.py \\",
            "  --repetitions 3 \\",
            "  --require-clean-worktree \\",
            "  --artifact-path benchmarks/results/runtime-contract-benchmark-v1-3x.json \\",
            "  --report-path docs/metrics/runtime-contract-benchmark-v1-3x.md",
            "```",
            "",
            "## Provenance",
            "",
            f"- Captured at: `{artifact['captured_at']}`",
            f"- Commit: `{runtime['commit_sha']}`",
            f"- Branch: `{runtime['branch']}`",
            f"- Working tree dirty before execution: `{runtime['working_tree_dirty']}`",
            f"- Manifest snapshot: `{benchmark['fixture_snapshot_id']}`",
            f"- Evaluation snapshot: `{benchmark['evaluation_snapshot_id']}`",
            "",
            "## Scope",
            "",
            "This is a deterministic runtime-contract regression, not a live-LLM coding benchmark. "
            "It supports claims about prompt budgeting, duplicate-read handling, checkpoint validation, "
            "and partial-success audit behavior for this frozen source snapshot only. It does not measure "
            "general task success, cross-session long-term memory retrieval, model cost, or a real-task "
            "read-count reduction rate.",
            "",
        ]
    )
    return "\n".join(lines)


@dataclass
class RuntimeContractBenchmarkRunner:
    benchmark_path: Path | str = DEFAULT_RUNTIME_CONTRACT_BENCHMARK_PATH
    artifact_path: Path | str = DEFAULT_RUNTIME_CONTRACT_ARTIFACT_PATH
    report_path: Path | str = DEFAULT_RUNTIME_CONTRACT_REPORT_PATH
    workspace_root: Path | str = DEFAULT_RUNTIME_CONTRACT_WORKSPACE_ROOT
    repetitions: int = 3
    require_clean_worktree: bool = False

    def __post_init__(self):
        self.benchmark_path = Path(self.benchmark_path).resolve()
        self.repo_root = self.benchmark_path.parent.parent.resolve()
        self.artifact_path = _resolve_from_root(self.artifact_path, self.repo_root)
        self.report_path = _resolve_from_root(self.report_path, self.repo_root)
        self.workspace_root = _resolve_from_root(self.workspace_root, self.repo_root)
        self.repetitions = int(self.repetitions)
        if self.repetitions < 1:
            raise ValueError("repetitions must be positive")

    def _preflight(self):
        if not self.require_clean_worktree:
            return
        status = git_value(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            fallback=None,
            preserve_empty=True,
        )
        if status is None:
            raise RuntimeError("cannot verify a clean worktree")
        if status:
            raise RuntimeError(
                "runtime-contract benchmark requires a clean worktree; "
                "commit, stash, or use a separate clean worktree first"
            )

    def _source_hashes(self):
        paths = (
            self.benchmark_path,
            self.repo_root / "pico/evaluation/runtime_contract_benchmark.py",
            self.repo_root / "scripts/run_runtime_contract_benchmark.py",
            self.repo_root / "pico/context_manager.py",
            self.repo_root / "pico/agent/checkpoints.py",
            self.repo_root / "pico/agent/state.py",
            self.repo_root / "pico/tools/runtime.py",
            self.repo_root / "pico/runtime.py",
            self.repo_root / "pico/config.py",
        )
        return {_relative(path, self.repo_root): _file_digest(path) for path in paths}

    def run(self, task_ids=None):
        benchmark = load_runtime_contract_benchmark(
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
                "unknown runtime contract task ids: " + ", ".join(sorted(unknown_ids))
            )
        self._preflight()
        source_hashes = self._source_hashes()
        run_id = "runtime-contract-" + uuid.uuid4().hex[:12]
        run_root = self.workspace_root / run_id
        rows = []
        for repetition in range(1, self.repetitions + 1):
            for task in tasks:
                case_root = run_root / task["id"] / f"rep-{repetition}"
                started_at = time.monotonic()
                try:
                    evidence = HANDLERS[task["handler"]](case_root, task)
                    verifier = dict(evidence["verifier"])
                    passed = bool(verifier.get("passed"))
                    error = ""
                except Exception as exc:  # keep the artifact reviewable on a failure
                    evidence = {
                        "control": {"name": task["control"], "metrics": {}},
                        "candidate": {"name": task["candidate"], "metrics": {}},
                    }
                    verifier = {
                        "name": f"{task['handler']}_contract_v1",
                        "passed": False,
                        "checks": [],
                    }
                    passed = False
                    error = f"{type(exc).__name__}: {str(exc)[:500]}"
                outcome = {
                    "task_id": task["id"],
                    "control": evidence["control"],
                    "candidate": evidence["candidate"],
                    "verifier": verifier,
                    "error": error,
                }
                rows.append(
                    {
                        "task_id": task["id"],
                        "family": task["family"],
                        "handler": task["handler"],
                        "acceptance": list(task["acceptance"]),
                        "repetition": repetition,
                        "workspace_id": f"{task['id']}/rep-{repetition}",
                        "duration_ms": int((time.monotonic() - started_at) * 1000),
                        "passed": passed,
                        "control": evidence["control"],
                        "candidate": evidence["candidate"],
                        "verifier": verifier,
                        "error": error,
                        "outcome_fingerprint": _digest(_stable_outcome(outcome)),
                    }
                )

        git_status = git_value(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            fallback=None,
            preserve_empty=True,
        )
        manifest_digest = _file_digest(self.benchmark_path)
        artifact = {
            "schema_version": RUNTIME_CONTRACT_ARTIFACT_SCHEMA_VERSION,
            "artifact_type": "pico-runtime-contract-benchmark",
            "execution_mode": "deterministic_scripted",
            "no_remote_model_calls": True,
            "captured_at": utc_timestamp(),
            "repetitions": self.repetitions,
            "runtime": {
                "commit_sha": git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": git_value(["branch", "--show-current"], cwd=self.repo_root),
                "working_tree_dirty": None if git_status is None else bool(git_status),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
            },
            "benchmark": {
                "name": benchmark["name"],
                "description": str(benchmark.get("description", "")).strip(),
                "source": _relative(self.benchmark_path, self.repo_root),
                "manifest_sha256": manifest_digest,
                "fixture_snapshot_id": _digest(
                    {"manifest_sha256": manifest_digest, "tasks": tasks}
                ),
                "evaluation_snapshot_id": _digest(
                    {
                        "manifest_sha256": manifest_digest,
                        "source_hashes": source_hashes,
                    }
                ),
                "source_hashes": source_hashes,
                "task_ids": [task["id"] for task in tasks],
            },
            "rows": rows,
            "summary": summarize_runtime_contract_rows(rows),
        }
        _atomic_write(
            self.artifact_path,
            json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write(self.report_path, render_runtime_contract_markdown(artifact))
        return artifact
