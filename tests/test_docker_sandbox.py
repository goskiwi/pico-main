import json
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pico import ModelAction, Pico, PicoConfig, SessionStore, Workspace
from pico.command_runner import CommandResult, shell_argv
from pico.docker_sandbox import DockerSandbox
from pico.execution import ExecutionContext
from pico.persistence import atomic_write_json
from tests.support import ScriptedModel, build_agent


def make_sandbox(root):
    session = SessionStore(root / ".pico" / "sessions").create(root)
    return DockerSandbox(root, session=session)


class DockerSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.sandbox = make_sandbox(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_container_policy_is_explicit_and_does_not_pass_secrets(self):
        args = self.sandbox._create_args("pico-" + "a" * 32, (), "/tmp/empty")
        for flag in (
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--pids-limit=128",
        ):
            self.assertIn(flag, args)
        self.assertIn("--security-opt=no-new-privileges", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--env-file", args)
        self.assertFalse(any("docker.sock" in arg for arg in args))
        with mock.patch.dict(os.environ, {"PICO_OPENAI_API_KEY": "test-secret"}):
            self.assertNotIn("test-secret", " ".join(args))
            self.assertNotIn("PICO_OPENAI_API_KEY", self.sandbox._docker_environment())

    def test_sensitive_paths_are_masked_but_examples_remain_visible(self):
        (self.root / ".env").write_text("test-secret")
        (self.root / ".env.example").write_text("sample")
        nested = self.root / "src"
        nested.mkdir()
        (nested / ".env.local").write_text("nested-secret")
        (self.root / ".git").mkdir()
        (self.root / ".venv").mkdir()
        masks = self.sandbox._protected_paths(ExecutionContext.root(max_seconds=2))
        kinds = {relative: kind for kind, relative, _directory in masks}
        self.assertEqual(kinds[".git"], "readonly")
        for name in (".pico", ".venv", ".env", "src/.env.local"):
            self.assertEqual(kinds[name], "hidden")
        self.assertNotIn(".env.example", kinds)

    def test_protected_symlink_is_rejected(self):
        (self.root / ".env").symlink_to("config.txt")
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            self.sandbox._protected_paths(ExecutionContext.root(max_seconds=2))

    def test_workspace_sockets_and_fifos_are_not_exposed_to_shell(self):
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.root / "service.sock"))
            os.mkfifo(self.root / "pipe")
            masks = self.sandbox._protected_paths(ExecutionContext.root(max_seconds=2))
            kinds = {relative: kind for kind, relative, _directory in masks}
            self.assertEqual(kinds["service.sock"], "hidden")
            self.assertEqual(kinds["pipe"], "hidden")

    def test_missing_docker_never_executes_the_host_command(self):
        with (
            mock.patch("pico.docker_sandbox.shutil.which", return_value=None),
            mock.patch.object(self.sandbox.host, "run") as host,
        ):
            result = self.sandbox.run(
                shell_argv("touch escaped.txt"),
                cwd=self.root,
                timeout=2,
            )
        self.assertTrue(result.infrastructure_error)
        host.assert_not_called()
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_missing_image_is_an_infrastructure_error_without_command_execution(self):
        with mock.patch.object(
            self.sandbox,
            "_control",
            side_effect=[
                CommandResult(0, "unix:///tmp/docker.sock"),
                CommandResult(0, "linux"),
                CommandResult(1, stderr="No such image"),
            ],
        ) as control:
            result = self.sandbox.run(shell_argv("touch x"), cwd=self.root, timeout=2)
        self.assertTrue(result.infrastructure_error)
        self.assertIn("image lookup", result.stderr)
        self.assertEqual(control.call_count, 3)
        self.assertFalse(self.sandbox.record.exists())

    def test_remote_docker_endpoint_is_rejected(self):
        with (
            mock.patch.dict(os.environ, {"DOCKER_HOST": "ssh://remote"}),
            mock.patch.object(self.sandbox, "_control") as control,
        ):
            result = self.sandbox.run(shell_argv("touch x"), cwd=self.root, timeout=2)
        self.assertTrue(result.infrastructure_error)
        control.assert_not_called()

    def test_cleanup_checks_exact_container_ownership(self):
        name = "pico-" + "a" * 32
        atomic_write_json(self.sandbox.record, {"container": name})
        with (
            mock.patch.object(
                self.sandbox,
                "_control",
                return_value=CommandResult(
                    0,
                    json.dumps([{"Config": {"Labels": {"pico.session": "foreign"}}}]),
                ),
            ) as control,
            self.assertRaisesRegex(RuntimeError, "ownership mismatch"),
        ):
            self.sandbox.close()
        self.assertEqual(control.call_count, 1)
        self.assertTrue(self.sandbox.record.exists())

    def test_cleanup_failure_keeps_record_for_next_startup(self):
        name = "pico-" + "a" * 32
        atomic_write_json(self.sandbox.record, {"container": name})
        with (
            mock.patch.object(
                self.sandbox,
                "_control",
                side_effect=[
                    CommandResult(
                        0, json.dumps([{"Config": {"Labels": self.sandbox.labels}}])
                    ),
                    CommandResult(1, stderr="daemon unavailable"),
                ],
            ),
            self.assertRaisesRegex(RuntimeError, "stop failed"),
        ):
            self.sandbox.close()
        self.assertTrue(self.sandbox.record.exists())

    def test_timeout_stops_container_not_just_docker_client(self):
        with (
            mock.patch.object(self.sandbox, "_ensure"),
            mock.patch.object(
                self.sandbox.host,
                "run",
                return_value=CommandResult(
                    None,
                    stdout="started",
                    stop_reason="deadline_exceeded",
                ),
            ),
            mock.patch.object(self.sandbox, "close") as close,
        ):
            result = self.sandbox.run(shell_argv("sleep 30"), cwd=self.root, timeout=2)
        self.assertEqual(result.stdout, "started")
        close.assert_called_once()

    def test_startup_removes_the_previous_container_before_run_observation(self):
        name = "pico-" + "a" * 32
        atomic_write_json(self.sandbox.record, {"container": name})
        with mock.patch.object(
            self.sandbox,
            "_control",
            side_effect=[
                CommandResult(
                    0, json.dumps([{"Config": {"Labels": self.sandbox.labels}}])
                ),
                CommandResult(0),
                CommandResult(0),
            ],
        ) as control:
            self.sandbox.reconcile()
        self.assertEqual(
            control.call_args_list[-2].args[0], ("stop", "--timeout", "1", name)
        )
        self.assertFalse(self.sandbox.record.exists())

    def test_cleanup_failure_does_not_mark_the_run_completed(self):
        agent, _model = build_agent(self.root, [ModelAction.final("Done.")])
        agent.dependencies.shell_runner.close = mock.Mock(
            side_effect=RuntimeError("container stop unconfirmed")
        )
        with self.assertRaisesRegex(RuntimeError, "stop unconfirmed"):
            agent.ask("Finish a task")
        self.assertEqual(agent.run.projection.status, "running")
        self.assertTrue(agent.session.active_run_id)
        self.assertFalse(
            any(
                event.kind == "assistant_turn"
                for event in agent.read_run_events(agent.run.projection.run_id)
            )
        )


@unittest.skipUnless(
    os.environ.get("PICO_DOCKER_TESTS") == "1",
    "set PICO_DOCKER_TESTS=1 for real containers",
)
class DockerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # An explicitly enabled integration run must FAIL, not skip, if Docker
        # or the image is unavailable.
        subprocess.run(("docker", "info"), capture_output=True, check=True, timeout=10)
        subprocess.run(
            ("docker", "image", "inspect", "pico-sandbox:python"),
            capture_output=True,
            check=True,
            timeout=10,
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.sandbox = make_sandbox(self.root)

    def tearDown(self):
        self.sandbox.close()
        self.temporary.cleanup()

    def execute(self, command, timeout=30, context=None):
        return self.sandbox.run(
            shell_argv(command),
            cwd=self.root,
            timeout=timeout,
            execution_context=context,
        )

    def test_live_workspace_reuse_and_linux_dependency_environment(self):
        (self.root / "source.txt").write_text("first")
        first = self.execute(
            "python -c \"from pathlib import Path; assert Path('source.txt').read_text() == 'first'; Path('/tmp/reused').write_text('yes'); Path('generated.txt').write_text('created')\""
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        name = self.sandbox._name
        (self.root / "source.txt").write_text("second")
        second = self.execute(
            "python -c \"from pathlib import Path; assert Path('source.txt').read_text() == 'second'; assert Path('/tmp/reused').read_text() == 'yes'\"; pytest --version"
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(name, self.sandbox._name)
        self.assertEqual((self.root / "generated.txt").read_text(), "created")
        self.sandbox.close()
        self.assertFalse(self.sandbox.record.exists())
        self.assertNotEqual(
            subprocess.run(
                ("docker", "inspect", name), capture_output=True, check=False
            ).returncode,
            0,
        )

    def test_protected_files_host_paths_network_and_privileges(self):
        (self.root / ".env").write_text("PRIVATE_TEST_VALUE")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("unchanged")
        (self.root / ".pico" / "private.txt").write_text("PRIVATE_TEST_VALUE")
        (self.root / "outside").symlink_to(self.root.parent)
        source = "\n".join(
            [
                "from pathlib import Path",
                "import os, socket",
                "assert Path('.env').read_text() == ''",
                "assert not Path('.pico/private.txt').exists()",
                f"assert not Path({str(self.root)!r}).exists()",
                "assert not Path('/var/run/docker.sock').exists()",
                "assert os.geteuid() != 0",
                "assert 'PICO_OPENAI_API_KEY' not in os.environ",
                "for path in ['/etc/pico-write-test', '.git/config', '.pico/pico-write-test', 'outside/pico-write-test']:",
                "    try: Path(path).write_text('escaped')",
                "    except OSError: pass",
                "    else: raise AssertionError(path)",
                "try: socket.create_connection(('1.1.1.1', 443), timeout=1)",
                "except OSError: pass",
                "else: raise AssertionError('network access')",
            ]
        )
        result = self.execute("python -c " + shlex.quote(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / ".git" / "config").read_text(), "unchanged")
        configured = json.loads(
            subprocess.run(
                ("docker", "inspect", self.sandbox._name),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout
        )[0]["HostConfig"]
        self.assertEqual(configured["NetworkMode"], "none")
        self.assertTrue(configured["ReadonlyRootfs"])
        self.assertFalse(configured["Privileged"])
        self.assertEqual(configured["CapDrop"], ["ALL"])
        self.assertEqual(configured["Memory"], 1024**3)
        self.assertEqual(configured["PidsLimit"], 128)

    def test_timeout_stops_descendants_but_preserves_already_written_files(self):
        warmup = self.execute("true")
        self.assertEqual(warmup.returncode, 0, warmup.stderr)
        name = self.sandbox._name
        result = self.execute(
            "printf started; echo before > generated.txt; (sleep 3; echo escaped > survived.txt) & wait",
            timeout=0.5,
        )
        self.assertEqual(result.stop_reason, "deadline_exceeded")
        self.assertIn("started", result.stdout)
        self.assertEqual((self.root / "generated.txt").read_text().strip(), "before")
        time.sleep(3)
        self.assertFalse((self.root / "survived.txt").exists())
        self.assertNotEqual(
            subprocess.run(
                ("docker", "inspect", name), capture_output=True, check=False
            ).returncode,
            0,
        )

    def test_cancellation_stops_container(self):
        warmup = self.execute("true")
        self.assertEqual(warmup.returncode, 0, warmup.stderr)
        context = ExecutionContext.root(max_seconds=30)
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                self.execute("printf started; sleep 30", context=context)
            )
        )
        thread.start()
        time.sleep(0.5)
        context.request_stop("user_cancelled")
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].stop_reason, "user_cancelled")
        self.assertFalse(self.sandbox.record.exists())

    def test_pico_ask_edits_locally_tests_in_docker_and_cleans_up(self):
        target = self.root / "calculator.py"
        target.write_text("def total(price, quantity):\n    return price\n")
        (self.root / "test_calculator.py").write_text(
            "from calculator import total\ndef test_quantity():\n    assert total(3, 4) == 12\n"
        )
        session = SessionStore(self.root / ".pico" / "sessions").create(self.root)
        model = ScriptedModel(
            [
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "calculator.py",
                        "old_text": "return price",
                        "new_text": "return price * quantity",
                    },
                ),
                ModelAction.tool("run_shell", {"command": "python -m pytest -q"}),
                ModelAction.final("Fixed and tested."),
            ]
        )
        agent = Pico(
            model,
            Workspace.build(self.root),
            session,
            config=PicoConfig(
                mode="auto", context_limit_tokens=32000, max_output_tokens=1000
            ),
            approval_handler=lambda *_args: True,
        )
        outcome = agent.ask("Fix quantity multiplication")
        self.assertEqual(outcome.status, "completed")
        events = agent.read_run_events(outcome.run_id)
        shell = next(
            event.payload["outcome"]
            for event in events
            if event.kind == "tool_result"
            and event.payload["outcome"]["tool_name"] == "run_shell"
        )
        self.assertEqual(shell["status"], "success", shell)
        self.assertEqual(shell["structured"]["executor"], "docker")
        self.assertIn("1 passed", shell["content"])
        self.assertFalse(agent.dependencies.shell_runner.record.exists())

    def test_recovered_owner_stops_old_container_instead_of_replaying(self):
        result = self.execute("echo preserved > generated.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        name = self.sandbox._name
        session = SessionStore(self.root / ".pico" / "sessions").load(
            self.sandbox.labels["pico.session"]
        )
        recovered = DockerSandbox(self.root, session=session)
        recovered.reconcile()
        self.assertEqual((self.root / "generated.txt").read_text().strip(), "preserved")
        self.assertFalse(recovered.record.exists())
        self.assertNotEqual(
            subprocess.run(
                ("docker", "inspect", name), capture_output=True, check=False
            ).returncode,
            0,
        )

    def test_docker_output_uses_existing_bounded_head_tail_collection(self):
        result = self.execute(
            "python -c \"import sys; sys.stdout.write('A' * 2000000); sys.stderr.write('B' * 2000000)\""
        )
        self.assertEqual(result.returncode, 0, result.stderr[-100:])
        self.assertTrue(result.output_limited)
        self.assertGreater(result.stdout_discarded_bytes, 0)
        self.assertGreater(result.stderr_discarded_bytes, 0)
        self.assertTrue(result.stdout.startswith("A") and result.stdout.endswith("A"))
        self.assertTrue(result.stderr.startswith("B") and result.stderr.endswith("B"))
        self.assertLess(len(result.stdout) + len(result.stderr), 1050000)
