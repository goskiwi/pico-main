import os

import pytest

from pico.sandbox import DockerSandbox

pytestmark = pytest.mark.docker


def docker_tests_enabled():
    return os.environ.get("PICO_RUN_DOCKER_TESTS") == "1"


@pytest.mark.skipif(not docker_tests_enabled(), reason="set PICO_RUN_DOCKER_TESTS=1")
def test_real_docker_sandbox_enforces_runtime_boundaries(tmp_path):
    (tmp_path / "visible.txt").write_text("public\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("CANARY=secret\n", encoding="utf-8")
    sandbox = DockerSandbox(tmp_path)

    inspect_script = (
        "from pathlib import Path; "
        "print(Path('visible.txt').read_text().strip()); "
        "print(Path('.env.local').read_text())"
    )
    inspect = sandbox.run(
        (
            "python",
            "-c",
            inspect_script,
        ),
        cwd=tmp_path,
        timeout=20,
    )
    assert inspect.returncode == 0
    assert inspect.stdout.splitlines() == ["public", ""]
    assert "secret" not in inspect.stdout

    write_attempt = sandbox.run(
        ("python", "-c", "from pathlib import Path; Path('escape.txt').write_text('x')"),
        cwd=tmp_path,
        timeout=20,
    )
    assert write_attempt.returncode != 0
    assert not (tmp_path / "escape.txt").exists()

    network_attempt = sandbox.run(
        (
            "python",
            "-c",
            "import socket; socket.create_connection(('1.1.1.1', 53), timeout=1)",
        ),
        cwd=tmp_path,
        timeout=20,
    )
    assert network_attempt.returncode != 0


@pytest.mark.skipif(not docker_tests_enabled(), reason="set PICO_RUN_DOCKER_TESTS=1")
def test_real_docker_prefers_workspace_src_over_installed_packages(tmp_path):
    package = tmp_path / "src" / "urllib3"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("SOURCE = 'workspace'\n", encoding="utf-8")
    sandbox = DockerSandbox(tmp_path)

    result = sandbox.run(
        (
            "python",
            "-c",
            "import urllib3; print(urllib3.SOURCE); print(urllib3.__file__)",
        ),
        cwd=tmp_path,
        timeout=20,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "workspace"
    assert result.stdout.splitlines()[1] == "/workspace/src/urllib3/__init__.py"
