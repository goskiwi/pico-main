"""Run the configured public tests in Docker through Pico's existing verifier.

The subprocess entry point uses only stdlib and runs with Python -I. It receives
source changes and public commands, never SWE-bench reference/test patches.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path, PurePosixPath


def capture_patch(workspace):
    """Capture tracked/new files with a temporary index; leave the real index alone."""
    with tempfile.TemporaryDirectory(prefix="pico-public-index-") as temporary:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        def git(*args):
            return subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
                cwd=workspace, env=environment, capture_output=True, check=True, timeout=30,
            ).stdout
        git("read-tree", "HEAD")
        git("add", "-A", "--", ".", ":(exclude).pico", ":(exclude).pico/**")
        return git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")


def public_script(base_commit, command):
    return (
        "#!/bin/bash\nset -eu\nexec 2>&1\n"
        "source /opt/miniconda3/bin/activate testbed\ncd /testbed\n"
        f"git -c core.hooksPath=/dev/null reset --hard {shlex.quote(base_commit)}\n"
        "if [ -s /candidate.patch ]; then git apply /candidate.patch; fi\n"
        + command + "\n"
    )


class DockerPublicVerifier:
    """Adapter for CommandRunner.run; execution/cancellation remain Runtime-owned."""
    def __init__(self, workspace, command, image, base_commit, output, *, docker_host=None,
                 check_directory="."):
        from pico.command_runner import CommandRunner

        self.workspace = Path(workspace).resolve()
        self.command = command
        self.image = image
        self.base_commit = base_commit
        self.output = Path(output).resolve()
        directory = PurePosixPath(check_directory)
        if directory.is_absolute() or ".." in directory.parts:
            raise ValueError("check_directory must stay inside the container repository")
        self.check_directory = directory.as_posix()
        if self.output.is_relative_to(self.workspace):
            raise ValueError("public verification logs must be outside the model workspace")
        self.docker_host = docker_host or subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        self.host_runner = CommandRunner(self.workspace)
        self.calls = []
        self.checks = []

    def run(self, argv, *, cwd, timeout, env=None, execution_context=None):
        from pico.command_runner import shell_argv

        if tuple(argv) != shell_argv(self.command) or Path(cwd).resolve() != self.workspace:
            raise ValueError("public verifier only accepts its configured command and workspace")
        return self._execute(timeout, execution_context)

    def run_check(self, *, code, kind, timeout_seconds, execution_context=None):
        if kind not in {"python", "pytest"} or not 1 <= len(code) <= 16000:
            raise ValueError("invalid diagnostic kind or code length")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("diagnostic timeout must be between 1 and 60 seconds")
        return self._execute(timeout_seconds, execution_context, code=code, kind=kind)

    def _execute(self, timeout, execution_context, *, code=None, kind="python"):
        diagnostic = code is not None
        records = self.checks if diagnostic else self.calls
        base = self.output / "diagnostics" if diagnostic else self.output
        output = base / f"check-{len(records) + 1:03d}"
        extra_args = []
        if diagnostic:
            output.mkdir(parents=True, exist_ok=False)
            snippet = output / "snippet.py"
            snippet.write_text(code)
            extra_args = ["--snippet", str(snippet), "--kind", kind,
                          "--check-directory", self.check_directory,
                          "--check-timeout", str(timeout)]
        name = "pico-public-" + uuid.uuid4().hex
        started = time.monotonic()
        try:
            result = self.host_runner.run([
                sys.executable, "-I", str(Path(__file__).resolve()),
                "--workspace", str(self.workspace), "--output", str(output),
                "--docker-host", self.docker_host, "--container", name,
                "--image", self.image, "--base", self.base_commit,
                "--command", self.command,
                *extra_args,
            ], cwd=self.workspace, timeout=timeout, env={}, execution_context=execution_context)
            report_path = output / "result.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            result = replace(result, infrastructure_error=(
                result.infrastructure_error or result.returncode == 125
            ), output_limited=result.output_limited or report.get("output_limited", False),
                returncode=(report["exit_code"]
                            if diagnostic and report and not report.get("output_limited")
                            else result.returncode))
            records.append({"seconds": round(time.monotonic() - started, 3),
                               "exit_code": result.returncode, "stop_reason": result.stop_reason,
                               "infrastructure_error": result.infrastructure_error,
                               "output_limited": result.output_limited,
                               "container": name,
                               "output": str(output)})
            return result
        finally:
            # Docker's daemon outlives the helper process group. Clean our known
            # container even if the helper was cancelled before it could report.
            subprocess.run(["docker", "--host", self.docker_host, "rm", "-f", name],
                           capture_output=True, check=False, timeout=10)


def diagnostic_script(base_commit, kind, directory, filename, timeout):
    destination = str(PurePosixPath("/testbed") / directory / filename)
    command = (
        f"python -m pytest -q --tb=short -r fE {shlex.quote(destination)}"
        if kind == "pytest" else
        f"env PYTHONPATH=/testbed/src:/testbed python {shlex.quote(destination)}"
    )
    # This also bounds orphaned checks if the host Runtime process is killed.
    command = f"timeout --signal=TERM --kill-after=1s {float(timeout):.3f}s " + command
    return public_script(base_commit,
                         f"cp /diagnostic.py {shlex.quote(destination)}\n" + command)


def bounded_diagnostic_output(argv, path, limit=256 * 1024):
    """Bound persisted output as well as memory for arbitrary diagnostic code."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    count = 0
    limited = False
    try:
        with selectors.DefaultSelector() as selector, path.open("wb") as log:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                for key, _mask in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    log.write(chunk[:max(0, limit - count)])
                    count += len(chunk)
                    if count > limit:
                        limited = True
                        return limited
        process.wait()
        return limited
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def run_public(args):
    args.output.mkdir(parents=True, exist_ok=True)
    docker = ["docker", "--host", args.docker_host]
    def cli(*argv):
        return subprocess.run([*docker, *map(str, argv)], capture_output=True, text=True,
                              check=True, timeout=60).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="pico-public-files-") as temporary:
        temporary = Path(temporary)
        (temporary / "candidate.patch").write_bytes(capture_patch(args.workspace))
        if args.snippet:
            script = diagnostic_script(args.base, args.kind, args.check_directory,
                                       "test_pico_diagnostic_" + uuid.uuid4().hex + ".py",
                                       args.check_timeout)
            (temporary / "diagnostic.py").write_bytes(args.snippet.read_bytes())
        else:
            script = public_script(args.base, args.command)
        (temporary / "public.sh").write_text(script)
        extra_flags = ["--log-driver", "none"] if args.snippet else []
        cli("create", "--pull=never", "--name", args.container, *extra_flags, "--platform", "linux/amd64",
            "--network", "none", "--memory", "4g", "--cpus", "2", "--pids-limit", "512",
            "--entrypoint", "/bin/bash", args.image, "/public.sh")
        files = ["candidate.patch", "public.sh"] + (["diagnostic.py"] if args.snippet else [])
        for name in files:
            cli("cp", temporary / name, f"{args.container}:/{name}")
        # The outer CommandRunner owns the total deadline and process group.
        log_path = args.output / "tests.log"
        limited = False
        if args.snippet:
            limited = bounded_diagnostic_output([*docker, "start", "-a", args.container], log_path)
            if limited:
                subprocess.run([*docker, "kill", args.container], capture_output=True,
                               check=False, timeout=10)
        else:
            with log_path.open("w") as log:
                subprocess.run([*docker, "start", "-a", args.container], stdout=log,
                               stderr=subprocess.STDOUT, check=False)
        state = json.loads(cli("inspect", args.container))[0]["State"]
        if state["Status"] != "exited" or state.get("Error"):
            raise ValueError("container did not finish executing the check")
        code = int(state["ExitCode"])
        text = log_path.read_text(errors="replace")
        report = {"command": "diagnostic:" + args.kind if args.snippet else args.command,
                  "exit_code": code, "output_limited": limited,
                  "hidden_test_patch_applied": False, "oom_killed": state.get("OOMKilled", False)}
        (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        label = "Diagnostic check" if args.snippet else "Public regression tests"
        passed = code == 0 and not limited
        print(f"{label} {'passed' if passed else 'failed'} (exit {code}).")
        if limited:
            print("Output exceeded 256 KiB; diagnostic stopped. Narrow the check.")
        print(text[-16000:] if args.snippet else text[-3200:])
        return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--snippet", type=Path)
    parser.add_argument("--kind", choices=["python", "pytest"], default="python")
    parser.add_argument("--check-directory", default=".")
    parser.add_argument("--check-timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        return run_public(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Public verifier infrastructure error: {type(exc).__name__}: {exc}")
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
