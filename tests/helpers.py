import subprocess
from types import SimpleNamespace

from pico.runtime import Pico
from pico.sandbox import SandboxResult
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext
from tests.fakes import FakeModelClient


class UnitTestSandbox:
    """Host-backed test double; production code has no local execution backend."""

    backend = "test"

    def __init__(self, workspace_root):
        self.workspace_root = workspace_root
        self.config = SimpleNamespace(image="unit-test-sandbox")

    def identity(self):
        return {
            "backend": self.backend,
            "image": self.config.image,
            "cpus": 1.0,
            "memory": "128m",
            "pids_limit": 32,
            "network": "test-only",
            "rootfs_read_only": False,
        }

    def audit_metadata(self, *, timed_out=False):
        return {
            "sandbox_backend": self.backend,
            "sandbox_image": self.config.image,
            "sandbox_network": "test-only",
            "sandbox_rootfs_read_only": False,
            "sandbox_cpus": 1.0,
            "sandbox_memory": "128m",
            "sandbox_pids_limit": 32,
            "sandbox_timed_out": bool(timed_out),
        }

    def run(self, command, *, cwd, timeout, env=None):
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return SandboxResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    feature_flags = {"llm_memory_extract": False, "llm_history_compaction": False}
    feature_flags.update(kwargs.pop("feature_flags", {}) or {})
    sandbox = kwargs.pop("sandbox", UnitTestSandbox(tmp_path))
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        feature_flags=feature_flags,
        sandbox=sandbox,
        **kwargs,
    )
