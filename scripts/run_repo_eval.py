"""Run paired Pico RepoMap experiments; keep inference separate from hidden judging."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from statistics import mean
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico import Pico, PicoConfig, SessionStore, Workspace
from pico.command_runner import shell_argv
from pico.config import load_project_env, provider_env
from pico.providers.clients import OpenAICompatibleModelClient
from pico.security import redact_text
from scripts.real_case_support import git_metadata
from scripts.repo_eval_tasks import CATALOG, command, selected_tasks, write_json
from scripts.repo_eval_verify import DockerPublicVerifier

VARIANTS = ("repomap_on", "repomap_off")
BUDGET = {
    "max_agent_turns": 32,
    "max_tool_executions": 64,
    "turn_timeout_seconds": 600,
    "max_new_tokens": 4096,
    "provider_context_limit_tokens": 65536,
    "compaction_reserve_tokens": 8192,
    "compaction_keep_recent_tokens": 16384,
}


def error_message(exc, client):
    parts = [str(exc).strip() or type(exc).__name__]
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            parts.append(detail)
    message = redact_text("\n".join(parts))
    key = getattr(client, "api_key", "")
    return (message.replace(key, "<redacted>") if key else message)[:2000]


class MeteredClient(OpenAICompatibleModelClient):
    def __init__(self, *args, measurements=None, purpose="agent", **kwargs):
        super().__init__(*args, **kwargs)
        self.measurements = measurements if measurements is not None else []
        self.purpose = purpose

    def new_isolated_client(self):
        return MeteredClient(
            self.model, self.base_url, self.api_key, self.temperature, self.timeout,
            measurements=self.measurements, purpose="compaction",
        )

    def complete_action(self, *args, **kwargs):
        start = time.monotonic()
        self.last_completion_metadata = {}
        error = None
        detail = None
        try:
            return super().complete_action(*args, **kwargs)
        except Exception as exc:
            error = type(exc).__name__
            detail = error_message(exc, self)
            raise
        finally:
            self.measurements.append({
                "purpose": self.purpose, "seconds": round(time.monotonic() - start, 3),
                "error": error, "error_message": detail, **self.last_completion_metadata,
            })


def usage_totals(requests):
    result = {"provider_requests": len(requests),
              "compaction_requests": sum(r["purpose"] == "compaction" for r in requests)}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [r.get(key) for r in requests]
        known = [v for v in values if isinstance(v, (int, float)) and v >= 0]
        result[key] = sum(known) if values and len(known) == len(values) else None
        result[f"reported_{key}"] = sum(known) if known else None
        result[f"missing_{key}_requests"] = len(values) - len(known)
    # Preserve the provider's total separately: compatible endpoints can return
    # a total that differs from input + output. Do not silently reconcile it.
    result["provider_total_tokens"] = result["total_tokens"]
    result["total_tokens"] = (
        result["input_tokens"] + result["output_tokens"]
        if result["input_tokens"] is not None and result["output_tokens"] is not None
        else None
    )
    result["provider_total_delta"] = (
        result["provider_total_tokens"] - result["total_tokens"]
        if result["provider_total_tokens"] is not None and result["total_tokens"] is not None
        else None
    )
    return result


def task_prompt(problem):
    return (
        "Fix the following repository issue. Preserve unrelated behavior.\n\n"
        + problem.strip()
        + "\n\nDo not modify tests, conftest.py, test configuration, or dependency files. "
        "Inspect the repository using the available read/search tools and change the "
        "implementation. There is no general shell in this Auto-mode experiment. "
        "When you call submit_final, the Runtime runs the configured public regression "
        "tests in a container. If they fail, inspect the feedback, repair your code, "
        "and submit again. When run_check is available, use temporary diagnostics to test "
        "the requested behavior and possible regressions; these snippets do not modify "
        "the repository's test files. Independent acceptance tests run only after the trial ends "
        "and their results are not fed back to you."
    )


def public_command(task):
    value = task.get("public_verification", "").strip()
    if not value:
        raise ValueError(f"public verification is not configured for {task['instance_id']}")
    return value


def forbidden_change(path):
    path = Path(path)
    return (
        any(part in {"tests", "testing", "test"} for part in path.parts)
        or path.name.startswith("test_") or path.name.endswith("_test.py")
        or path.name in {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                         "pyproject.toml", "setup.py", "requirements.txt"}
        or path.name.startswith("requirements")
    )


def repository_patch(workspace):
    command(["git", "add", "-N", "--", ".", ":(exclude).pico"], cwd=workspace)
    patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", ".", ":(exclude).pico"],
        cwd=workspace, capture_output=True, text=True, check=True, timeout=30,
    ).stdout
    paths = command(["git", "diff", "--name-only", "HEAD", "--", ".",
                     ":(exclude).pico"], cwd=workspace).splitlines()
    return patch, paths


def run_trial(task, source, destination, client, config, repeat, variant,
              *, command_runner_factory=None):
    trial_id = f"{task['instance_id']}__r{repeat}__{variant}"
    directory = destination / trial_id
    directory.mkdir(parents=True)
    workspace = directory / "workspace"
    started = time.monotonic()
    agent = None
    verifier = None
    phase = "setup"
    row = {
        "trial_id": trial_id, "instance_id": task["instance_id"], "variant": variant,
        "repeat": repeat, "runtime_status": None, "stop_reason": None,
        "generation_error": None, "setup_error": None, "human_interventions": 0,
        "intervention_policy": "unattended; no guidance, retries or manual patching",
    }
    try:
        command(["git", "-c", "core.hooksPath=/dev/null", "clone", "--no-hardlinks",
                 source / "source", workspace])
        problem = (source / "problem.txt").read_text()
        prompt = task_prompt(problem)
        (directory / "prompt.txt").write_text(prompt)
        verification = public_command(task)
        if command_runner_factory is None:
            controls = json.loads((source / "controls.json").read_text())
            if not controls["valid"]:
                raise ValueError("independent task environment is not valid")
            verifier = DockerPublicVerifier(
                workspace, verification, controls["reference"]["image_id"],
                task["base_commit"], directory / "public-verification",
                check_directory=task.get("check_directory", "."),
            )
        else:
            verifier = command_runner_factory(workspace)
        setup = verifier.run(shell_argv(verification), cwd=workspace, timeout=120, env={})
        if setup.returncode != 0 or setup.stop_reason or setup.infrastructure_error:
            raise RuntimeError("original public tests failed: " + (setup.stdout + setup.stderr)[-3000:])
        started = time.monotonic()
        agent = Pico(
            client, Workspace.build(workspace),
            SessionStore(workspace / ".pico/sessions").create(workspace),
            config=replace(config, repo_map_enabled=variant == "repomap_on",
                           verification_command=verification),
            command_runner=verifier,
            check_runner=getattr(verifier, "run_check", None),
        )
        phase = "inference"
        outcome = agent.ask(prompt)
        row.update(runtime_status=outcome.status, stop_reason=outcome.stop_reason)
        (directory / "answer.txt").write_text(outcome.answer)
    except Exception as exc:  # noqa: BLE001 - retain failed trials in the experiment
        row["setup_error" if phase == "setup" else "generation_error"] = type(exc).__name__
        row["error_message"] = error_message(exc, client)
    finally:
        row["inference_seconds"] = round(time.monotonic() - started, 3)
        row.update(usage_totals(client.measurements))
        write_json(directory / "requests.json", client.measurements)
    events = agent.run.run_log.events if agent and agent.run.run_log else ()
    row["tool_failure_counts"] = dict(Counter(
        event.payload["outcome"]["failure"]["code"] for event in events
        if event.kind == "tool_result" and event.payload["outcome"].get("failure")
    ))
    row["compaction_count"] = sum(event.kind == "compaction" for event in events)
    row["executed_tools"] = sum(event.kind == "tool_started" for event in events)
    row["public_verification_results"] = [
        event.payload["status"] for event in events if event.kind == "verification_result"
    ]
    row["public_verification_calls"] = getattr(verifier, "calls", [])
    row["diagnostic_check_calls"] = getattr(verifier, "checks", [])
    patch, paths = ("", [])
    if (workspace / ".git").exists():
        try:
            patch, paths = repository_patch(workspace)
        except (OSError, subprocess.SubprocessError) as exc:
            row["setup_error"] = "patch_capture:" + type(exc).__name__
            row["error_message"] = error_message(exc, client)
    row["changed_paths"] = paths
    row["scope_violations"] = [path for path in paths if forbidden_change(path)]
    row["patch_nonempty"] = bool(patch)
    (directory / "candidate.patch").write_text(patch)
    write_json(directory / "result.json", row)
    return row, {"trial_id": trial_id, "instance_id": task["instance_id"],
                 "model_name_or_path": client.model, "model_patch": patch}


def summarize(rows, judgments):
    by_id = {r["trial_id"]: r for r in judgments}
    merged = []
    for row in rows:
        result = {**row}
        judgment = by_id.get(row["trial_id"])
        result["resolved"] = None
        if row.get("setup_error"):
            result["failure_reason"] = "trial_setup:" + row["setup_error"]
        elif judgment is None:
            result["failure_reason"] = "not_judged"
        elif judgment.get("infrastructure_error"):
            result["failure_reason"] = judgment["infrastructure_error"]
        else:
            result["resolved"] = bool(judgment["resolved"] and not row["scope_violations"])
            result["failure_reason"] = failure_reason(row, judgment)
        result["runtime_completed_and_resolved"] = (
            result["resolved"] is True and row["runtime_status"] == "completed"
        )
        merged.append(result)
    summary = {}
    for variant in VARIANTS:
        group = [row for row in merged if row["variant"] == variant]
        scored = [row for row in group if row["resolved"] is not None]
        complete_usage = [row for row in group if row["total_tokens"] is not None]
        summary[variant] = {
            "attempted": len(group), "scored": len(scored),
            "unscored": len(group) - len(scored),
            "resolved": sum(row["resolved"] is True for row in group),
            "success_rate_scored": mean(row["resolved"] for row in scored) if scored else None,
            "runtime_completed_and_resolved": sum(row["runtime_completed_and_resolved"] for row in group),
            "mean_inference_seconds": mean(row["inference_seconds"] for row in group) if group else None,
            "mean_total_tokens_complete_usage_only": mean(row["total_tokens"] for row in complete_usage) if complete_usage else None,
            "complete_usage_trials": len(complete_usage),
            "human_interventions": sum(row["human_interventions"] for row in group),
            "failure_reasons": dict(Counter(row["failure_reason"] for row in group if row["failure_reason"])),
        }
    pairs = {}
    for row in merged:
        pairs.setdefault((row["instance_id"], row["repeat"]), {})[row["variant"]] = row
    valid_pairs = [pair for pair in pairs.values()
                   if all(v in pair and pair[v]["resolved"] is not None for v in VARIANTS)]
    token_pairs = [pair for pair in valid_pairs
                   if all(pair[v]["total_tokens"] is not None for v in VARIANTS)]
    error_free_pairs = [pair for pair in valid_pairs
                        if all(not pair[v]["generation_error"] for v in VARIANTS)]
    summary["paired"] = {
        "scored_pairs": len(valid_pairs),
        "on_only_resolved": sum(p["repomap_on"]["resolved"] and not p["repomap_off"]["resolved"] for p in valid_pairs),
        "off_only_resolved": sum(p["repomap_off"]["resolved"] and not p["repomap_on"]["resolved"] for p in valid_pairs),
        "both_resolved": sum(all(p[v]["resolved"] for v in VARIANTS) for p in valid_pairs),
        "neither_resolved": sum(not any(p[v]["resolved"] for v in VARIANTS) for p in valid_pairs),
        "mean_seconds_on_minus_off": mean(
            p["repomap_on"]["inference_seconds"] - p["repomap_off"]["inference_seconds"]
            for p in valid_pairs
        ) if valid_pairs else None,
        "complete_usage_pairs": len(token_pairs),
        "mean_tokens_on_minus_off": mean(
            p["repomap_on"]["total_tokens"] - p["repomap_off"]["total_tokens"]
            for p in token_pairs
        ) if token_pairs else None,
        "pairs_without_generation_errors": len(error_free_pairs),
        "on_only_resolved_without_generation_errors": sum(
            p["repomap_on"]["resolved"] and not p["repomap_off"]["resolved"]
            for p in error_free_pairs
        ),
        "off_only_resolved_without_generation_errors": sum(
            p["repomap_off"]["resolved"] and not p["repomap_on"]["resolved"]
            for p in error_free_pairs
        ),
    }
    return merged, summary


def failure_reason(row, judgment):
    if row["scope_violations"]:
        return "forbidden_test_or_config_change"
    if judgment["resolved"]:
        return None
    if row["generation_error"]:
        return "generation_error:" + row["generation_error"]
    if row["runtime_status"] != "completed":
        return "runtime_stopped:" + str(row["stop_reason"])
    if not row["patch_nonempty"]:
        return "empty_patch"
    if judgment.get("patch_rejected"):
        return "patch_rejected"
    if judgment.get("evaluation_error"):
        return judgment["evaluation_error"]
    return "independent_tests_failed"


def write_summary(output, judgments=()):
    rows = json.loads((output / "results.json").read_text())
    merged, summary = summarize(rows, judgments)
    write_json(output / "summary.json", summary)
    with (output / "results.csv").open("w", newline="") as handle:
        columns = ["instance_id", "repeat", "variant", "runtime_status", "resolved",
                   "inference_seconds", "input_tokens", "output_tokens", "total_tokens",
                   "provider_total_tokens", "provider_total_delta",
                   "reported_input_tokens", "reported_output_tokens",
                   "missing_input_tokens_requests", "missing_output_tokens_requests",
                   "human_interventions", "failure_reason"]
        writer = csv.DictWriter(handle, columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(merged)
    lines = ["# Pico RepoMap 小样本对照", "",
             "人工选择的 SWE-bench Verified 子集；不是官方全量成绩。缺失 Token 不按 0 处理。",
             "修复成功由离线独立测试判定；Runtime 完成状态单独保留。", "",
             "| 组别 | 已运行 | 已判分 | 修复成功 | 平均推理秒 | Token 完整记录 |",
             "|---|---:|---:|---:|---:|---:|"]
    for variant in VARIANTS:
        s = summary[variant]
        seconds = s["mean_inference_seconds"]
        lines.append(f"| {variant} | {s['attempted']} | {s['scored']} | {s['resolved']} | "
                     f"{round(seconds, 2) if seconds is not None else '—'} | {s['complete_usage_trials']} |")
    lines.extend(["", f"成对结果：`{json.dumps(summary['paired'], ensure_ascii=False)}`", "",
                  "样本量小且为人工挑选，结果只用于分析这些任务；不能据此宣称普遍收益或统计显著。",
                  "逐任务失败与用量见 results.csv；异常、工具失败与原始调用用量见各 trial 文件。"])
    (output / "report.md").write_text("\n".join(lines) + "\n")


def snapshot_runtime(output):
    write_json(output / "runtime-git.json", git_metadata())
    with zipfile.ZipFile(output / "runtime-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("pico", "applications", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                archive.write(path, path.relative_to(ROOT))
        for name in ("pyproject.toml", "uv.lock", "benchmarks/repo_eval/tasks.json"):
            archive.write(ROOT / name, name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "summarize"])
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--cache", type=Path, default=ROOT / "artifacts/repo-eval-cache")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    parser.add_argument("--model")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--judgments", type=Path)
    args = parser.parse_args(argv)
    if args.action == "summarize":
        write_summary(args.output, json.loads(args.judgments.read_text()) if args.judgments else [])
        return 0
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if not args.ids:
        parser.error("run requires explicit --ids; do not launch an implicit full sweep")
    if len(args.variants) != len(set(args.variants)):
        parser.error("--variants must not contain duplicates")
    load_project_env(ROOT, boundary=ROOT)
    model = args.model or provider_env("PICO_OPENAI_MODEL")
    api_key = provider_env("PICO_OPENAI_API_KEY")
    base_url = provider_env("PICO_OPENAI_API_BASE")
    if not model or not api_key or not base_url:
        parser.error("set PICO_OPENAI_MODEL, PICO_OPENAI_API_KEY and PICO_OPENAI_API_BASE")
    catalog = json.loads(args.catalog.read_text())
    tasks = selected_tasks(catalog, args.ids)
    for task in tasks:
        public_command(task)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    endpoint = urlsplit(base_url)
    write_json(output / "experiment.json", {
        "dataset": catalog["dataset"], "dataset_revision": catalog["dataset_revision"],
        "task_ids": [t["instance_id"] for t in tasks], "model": model,
        "provider_host": endpoint.hostname, "temperature": args.temperature,
        "budget": BUDGET, "repeats": args.repeats,
        "protocol": "public-feedback-v3; Auto single-agent; optional isolated diagnostics; public Runtime verifier; independent offline acceptance",
        "variants": args.variants,
        "public_commands": {task["instance_id"]: public_command(task) for task in tasks},
        "repo_map_budget_tokens": 1200, "compaction": "same configuration in both arms",
        "total_token_policy": "reported consumption; not a hard cumulative billing limit",
        "status": "local experiment; working source snapshot included",
    })
    snapshot_runtime(output)
    config = PicoConfig(mode="auto", **BUDGET)
    results, predictions = [], []
    for repeat in range(1, args.repeats + 1):
        for index, task in enumerate(tasks):
            variants = args.variants if (index + repeat) % 2 else list(reversed(args.variants))
            for variant in variants:
                print(f"running {task['instance_id']} r{repeat} {variant}", flush=True)
                client = MeteredClient(model, base_url, api_key, args.temperature, 180)
                row, prediction = run_trial(
                    task, args.cache.resolve() / task["instance_id"], output,
                    client, config, repeat, variant,
                )
                results.append(row)
                predictions.append(prediction)
                write_json(output / "results.json", results)
                (output / "predictions.jsonl").write_text(
                    "".join(json.dumps(p) + "\n" for p in predictions)
                )
                write_summary(output)
                print(f"finished {row['trial_id']}: {row['runtime_status']}; "
                      f"tokens={row['total_tokens']}", flush=True)
                if row["setup_error"] or row["generation_error"]:
                    print("Batch stopped after setup/model failure; remaining trials were not run.", flush=True)
                    return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
