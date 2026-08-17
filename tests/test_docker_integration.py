import os

import pytest

from pico.sandbox import DockerSandbox, SandboxProfile

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
        profile=SandboxProfile.INSPECT,
    )
    assert inspect.returncode == 0
    assert inspect.stdout.splitlines() == ["public", ""]
    assert "secret" not in inspect.stdout

    write_attempt = sandbox.run(
        ("python", "-c", "from pathlib import Path; Path('escape.txt').write_text('x')"),
        cwd=tmp_path,
        timeout=20,
        profile=SandboxProfile.INSPECT,
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
        profile=SandboxProfile.INSPECT,
    )
    assert network_attempt.returncode != 0
