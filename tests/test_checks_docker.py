"""Explicit Docker integration checks; no downloads and no paid model calls.

PICO_TEST_DOCKER=1 uv run pytest -q tests/test_checks_docker.py
Requires the already prepared pytest-10051 source and instance image.
"""
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from pico.execution import ExecutionContext
from scripts.repo_eval_tasks import command
from scripts.repo_eval_verify import DockerPublicVerifier

pytestmark = pytest.mark.skipif(os.environ.get("PICO_TEST_DOCKER") != "1",
                                reason="opt-in integration check using prepared Docker image")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path):
    cache = ROOT / "artifacts/repo-eval-cache/pytest-dev__pytest-10051"
    if not (cache / "controls.json").is_file():
        pytest.fail("Prepare and validate pytest-dev__pytest-10051 before opting in")
    info = json.loads((cache / "instance.json").read_text())
    controls = json.loads((cache / "controls.json").read_text())
    workspace = tmp_path / "workspace"
    command(["git", "-c", "core.hooksPath=/dev/null", "clone", "--no-hardlinks",
             cache / "source", workspace])
    backend = DockerPublicVerifier(
        workspace, "python -m pytest -q testing/logging/test_fixture.py",
        controls["reference"]["image_id"], info["base_commit"], tmp_path / "checks",
        check_directory="testing/logging",
    )
    yield workspace, backend, info
    for check in backend.checks:
        result = subprocess.run(["docker", "--host", backend.docker_host, "inspect",
                                 check["container"]], capture_output=True, check=False, timeout=10)
        assert result.returncode != 0, "diagnostic container was not removed"


@pytest.mark.parametrize("variant,passed", [("original", True), ("bad_copy", False), ("reference", True)])
def test_retained_reference_regression(prepared, variant, passed):
    workspace, backend, info = prepared
    path = workspace / "src/_pytest/logging.py"
    if variant == "bad_copy":
        text = path.read_text().replace("self.records = []", "self.records.clear()", 1)
        text = text.replace("            yield\n\n            log = report_handler.stream",
                            "            yield\n\n            item.stash[caplog_records_key][when] = caplog_handler.records.copy()\n\n            log = report_handler.stream", 1)
        path.write_text(text)
    elif variant == "reference":
        patch = workspace.parent / "reference.patch"
        patch.write_text(info["patch"])
        command(["git", "apply", patch], cwd=workspace)
    before = path.read_bytes()
    code = (ROOT / "benchmarks/repo_eval/checks/retained_setup_reference.py").read_text()
    result = backend.run_check(code=code, kind="pytest", timeout_seconds=10)
    assert (result.returncode == 0) is passed
    assert path.read_bytes() == before


def test_isolation_limits_and_cancellation(prepared):
    workspace, backend, _info = prepared
    formal = workspace / "testing/logging/test_fixture.py"
    before = formal.read_bytes()
    result = backend.run_check(kind="python", timeout_seconds=10, code=(
        "import os\nfrom pathlib import Path\n"
        "assert 'PICO_OPENAI_API_KEY' not in os.environ\n"
        "Path('testing/logging/test_fixture.py').write_text('container only')\n"
    ))
    assert result.returncode == 0 and formal.read_bytes() == before
    result = backend.run_check(kind="python", timeout_seconds=2,
                               code="import time; time.sleep(30)")
    assert result.stop_reason == "deadline_exceeded"
    context = ExecutionContext.root(max_seconds=20)
    timer = threading.Timer(1.5, lambda: context.request_stop("test_cancelled"))
    timer.start()
    try:
        result = backend.run_check(kind="python", timeout_seconds=10,
                                   code="import time; time.sleep(30)", execution_context=context)
    finally:
        timer.cancel()
    assert result.stop_reason == "test_cancelled"
    result = backend.run_check(kind="python", timeout_seconds=10,
                               code="while True: print('x' * 8192, flush=True)")
    assert result.returncode != 0 and result.output_limited
    assert (Path(backend.checks[-1]["output"]) / "tests.log").stat().st_size <= 256 * 1024
    result = backend.run_check(kind="python", timeout_seconds=10, code="raise SystemExit(7)")
    assert result.returncode == 7


def test_container_watchdog_does_not_depend_on_runtime_parent(prepared):
    workspace, backend, _info = prepared
    snippet = workspace.parent / "sleep.py"
    snippet.write_text("import time; time.sleep(30)\n")
    output = workspace.parent / "watchdog"
    container = "pico-watchdog-test-" + uuid.uuid4().hex
    try:
        completed = subprocess.run([
            sys.executable, "-I", str(ROOT / "scripts/repo_eval_verify.py"),
            "--workspace", str(workspace), "--output", str(output),
            "--docker-host", backend.docker_host, "--container", container,
            "--image", backend.image, "--base", backend.base_commit,
            "--command", backend.command, "--snippet", str(snippet),
            "--check-timeout", "1", "--kind", "python",
        ], capture_output=True, text=True, timeout=10, check=False)
        assert completed.returncode == 1
        report = json.loads((output / "result.json").read_text())
        assert report["exit_code"] == 124  # GNU timeout, with no Runtime parent supervising it.
    finally:
        subprocess.run(["docker", "--host", backend.docker_host, "rm", "-f", container],
                       capture_output=True, check=False, timeout=10)
