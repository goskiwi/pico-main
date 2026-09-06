"""Prepare a small SWE-bench subset and judge patches in disposable Docker containers.

Run with benchmarks/repo_eval/requirements.txt in a separate Python environment.
No repository code is executed on the host; no credentials or host mounts enter Docker.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "benchmarks/repo_eval/tasks.json"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def command(argv, *, cwd=None, timeout=120):
    return subprocess.run(
        list(map(str, argv)), cwd=cwd, capture_output=True, text=True,
        check=True, timeout=timeout,
    ).stdout.strip()


def subprocess_error_detail(exc):
    detail = (getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or "")
    detail = detail.decode("utf-8", errors="replace") if isinstance(detail, bytes) else str(detail)
    return "\n".join(filter(None, [str(exc).strip(), detail.strip()]))[-4000:]


def selected_tasks(catalog, ids):
    tasks = catalog["tasks"]
    if not ids:
        return tasks
    unknown = set(ids) - {task["instance_id"] for task in tasks}
    if unknown:
        raise ValueError(f"unknown task ids: {sorted(unknown)}")
    return [task for task in tasks if task["instance_id"] in ids]


def dataset_rows(catalog, cache):
    from pyarrow import parquet

    revision = catalog["dataset_revision"]
    path = cache / f"dataset-{revision}.parquet"
    if not path.exists():
        url = (f"https://huggingface.co/datasets/{catalog['dataset']}/resolve/"
               f"{revision}/data/test-00000-of-00001.parquet")
        with urllib.request.urlopen(url, timeout=90) as response:
            payload = response.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return {row["instance_id"]: row for row in parquet.read_table(path).to_pylist()}


def prepare(catalog, cache, ids):
    rows = dataset_rows(catalog, cache)
    for task in selected_tasks(catalog, ids):
        task_id = task["instance_id"]
        row = rows[task_id]
        if (row["repo"], row["base_commit"]) != (task["repo"], task["base_commit"]):
            raise ValueError(f"catalog does not match dataset revision: {task_id}")
        directory = cache / task_id
        directory.mkdir(parents=True, exist_ok=True)
        # These files stay outside every model workspace.
        write_json(directory / "instance.json", row)
        (directory / "problem.txt").write_text(row["problem_statement"])
        source = directory / "source"
        if not source.exists():
            command(["git", "init", source])
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True,
            check=False, timeout=10,
        )
        if head.stdout.strip() != row["base_commit"]:
            command(["git", "-c", "core.hooksPath=/dev/null", "fetch", "--depth=1",
                     f"https://github.com/{row['repo']}.git", row["base_commit"]],
                    cwd=source, timeout=300)
            command(["git", "-c", "core.hooksPath=/dev/null", "checkout", "--detach",
                     row["base_commit"]], cwd=source)
        print(f"prepared {task_id}", flush=True)


def evaluate(instance, patch, output, *, timeout=300):
    from swebench.harness.grading import get_eval_report, get_logs_eval
    from swebench.harness.test_spec.test_spec import make_test_spec

    output.mkdir(parents=True, exist_ok=True)
    spec = make_test_spec(instance, namespace="swebench", arch="x86_64")
    image = spec.instance_image_key
    start = time.monotonic()
    container = None
    report = {"instance_id": instance["instance_id"], "image": image,
              "resolved": False, "infrastructure_error": None}
    try:
        try:
            image_id = command(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        except subprocess.CalledProcessError:
            print(f"pulling {image}", flush=True)
            command(["docker", "pull", "--platform", "linux/amd64", image], timeout=1200)
            image_id = command(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        report["image_id"] = image_id
        with tempfile.TemporaryDirectory(prefix="pico-judge-") as temporary:
            directory = Path(temporary)
            (directory / "candidate.patch").write_text(patch)
            (directory / "tests.sh").write_text(spec.eval_script)
            (directory / "run.sh").write_text(
                "#!/bin/bash\nset -e\ncd /testbed\n"
                f"git -c core.hooksPath=/dev/null reset --hard {instance['base_commit']}\n"
                "if [ -s /candidate.patch ]; then\n"
                "  git apply /candidate.patch || { echo PICO_PATCH_REJECTED; exit 92; }\n"
                "fi\nbash /tests.sh\n"
            )
            container = command([
                "docker", "create", "--platform", "linux/amd64", "--network", "none",
                "--memory", "4g", "--cpus", "2", "--pids-limit", "512",
                "--env", "PYTEST_ADDOPTS=-v -rA",
                "--entrypoint", "/bin/bash", image_id, "/run.sh",
            ])
            for name in ("candidate.patch", "tests.sh", "run.sh"):
                command(["docker", "cp", directory / name, f"{container}:/{name}"])
            run = subprocess.run(
                ["docker", "start", "-a", container], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                timeout=timeout, check=False,
            )
            log = run.stdout
            log_path = output / "tests.log"
            log_path.write_text(log)
            parsed = get_eval_report(
                spec, {"instance_id": instance["instance_id"], "model_patch": patch,
                       "model_name_or_path": "pico-local-eval"}, str(log_path), True,
            )[instance["instance_id"]]
            report.update(parsed)
            status_map, _ = get_logs_eval(spec, str(log_path))
            report["observed_test_results"] = len(status_map)
            report["container_exit_code"] = run.returncode
            report["patch_rejected"] = "PICO_PATCH_REJECTED" in log
            if not status_map and not report["patch_rejected"]:
                report["evaluation_error"] = "no_test_results"
    except subprocess.TimeoutExpired:
        if container:
            report["evaluation_error"] = "evaluation_timeout"
        else:
            report["infrastructure_error"] = "image_setup_timeout"
    except (OSError, subprocess.CalledProcessError) as exc:
        report["infrastructure_error"] = type(exc).__name__
        report["error_detail"] = subprocess_error_detail(exc)
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True,
                           check=False, timeout=30)
        report["evaluation_seconds"] = round(time.monotonic() - start, 3)
        write_json(output / "report.json", report)
    return report


def validate_controls(cache, task, output, timeout):
    instance = json.loads((cache / task["instance_id"] / "instance.json").read_text())
    before = evaluate(instance, "", output / "before", timeout=timeout)
    reference = evaluate(instance, instance["patch"], output / "reference", timeout=timeout)
    status = before.get("tests_status", {})
    f2p = status.get("FAIL_TO_PASS", {})
    p2p = status.get("PASS_TO_PASS", {})
    expected = instance["FAIL_TO_PASS"]
    expected = json.loads(expected) if isinstance(expected, str) else expected
    valid = (
        not before["infrastructure_error"] and not reference["infrastructure_error"]
        and not before["resolved"] and reference["resolved"]
        and len(f2p.get("failure", [])) == len(expected) and bool(expected)
        and not p2p.get("failure", [])
    )
    result = {"instance_id": task["instance_id"], "valid": bool(valid),
              "before": before, "reference": reference}
    write_json(output / "controls.json", result)
    write_json(cache / task["instance_id"] / "controls.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "validate", "judge"])
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--cache", type=Path, default=ROOT / "artifacts/repo-eval-cache")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    catalog = json.loads(args.catalog.read_text())
    tasks = selected_tasks(catalog, args.ids)
    if args.action == "prepare":
        prepare(catalog, args.cache, args.ids)
        return 0
    write_json(args.output / "environment.json", {
        "python": platform.python_version(), "host_platform": platform.platform(),
        "dataset": catalog["dataset"], "dataset_revision": catalog["dataset_revision"],
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "container_platform": "linux/amd64", "network": "none", "memory": "4g",
        "cpus": 2, "pids_limit": 512, "pytest_addopts": "-v -rA",
    })
    if args.action == "validate":
        results = []
        for task in tasks:
            result = validate_controls(args.cache, task,
                                       args.output / task["instance_id"], args.timeout)
            results.append(result)
            print(f"controls {task['instance_id']}: {result['valid']}", flush=True)
        return 0 if all(row["valid"] for row in results) else 1
    if args.predictions is None:
        parser.error("judge requires --predictions")
    predictions = [json.loads(line) for line in args.predictions.read_text().splitlines()]
    reports = []
    for prediction in predictions:
        if args.ids and prediction["instance_id"] not in args.ids:
            continue
        instance = json.loads((args.cache / prediction["instance_id"] / "instance.json").read_text())
        report = evaluate(instance, prediction["model_patch"],
                          args.output / prediction["trial_id"], timeout=args.timeout)
        controls_path = args.cache / prediction["instance_id"] / "controls.json"
        controls = json.loads(controls_path.read_text()) if controls_path.exists() else {}
        report["environment_controls_valid"] = controls.get("valid", False)
        if not report["environment_controls_valid"]:
            report["infrastructure_error"] = "unvalidated_task_environment"
        write_json(args.output / prediction["trial_id"] / "report.json", report)
        reports.append({"trial_id": prediction["trial_id"], **report})
        write_json(args.output / "judgments.json", reports)
        print(f"judged {prediction['trial_id']}: resolved={report['resolved']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
