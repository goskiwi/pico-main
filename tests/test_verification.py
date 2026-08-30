from evals.pytest_output import parse_pytest_output
from pico.evidence import verification_is_current
from pico.mutations import file_revision
from pico.sandbox import SandboxResult
from pico.verification import verify_workspace

READ_TASK = {
    "task_kind": "read_only",
    "requires_workspace_change": False,
    "requires_verification": False,
}
NO_CHANGE_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": False,
    "requires_verification": False,
}
MODIFY_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": True,
    "requires_verification": False,
}


def test_pytest_output_is_structured():
    output = """collected 4 items
tests/test_a.py ...F
FAILED tests/test_a.py::test_four - AssertionError
1 failed, 3 passed in 0.21s
"""

    first = parse_pytest_output(output)

    assert first["collected"] == 4
    assert first["passed"] == 3
    assert first["failed"] == 1
    assert first["failed_tests"] == ["tests/test_a.py::test_four"]


def test_double_quiet_pytest_progress_is_counted():
    result = parse_pytest_output(
        "..                                                                       [100%]"
    )
    assert result["collected"] == 2
    assert result["passed"] == 2


def test_runtime_verification_uses_configured_timeout_and_minimal_result(tmp_path):
    recorded = {}

    class Sandbox:
        def run(self, argv, **kwargs):
            recorded["argv"] = argv
            recorded.update(kwargs)
            return SandboxResult(returncode=0, stdout="2 passed")

    result = verify_workspace(
        root=tmp_path,
        command="MODE=test python -m pytest -q",
        sandbox=Sandbox(),
        timeout_seconds=600,
        redact_text=str,
        mutation_sequence_provider=lambda: 7,
        started_workspace_mutation_sequence=7,
        changed_paths=(),
    )

    assert recorded["argv"] == (
        "/bin/sh",
        "-c",
        "MODE=test python -m pytest -q",
    )
    assert recorded["env"] == {}
    assert recorded["timeout"] == 600
    assert result == {
        "command": "MODE=test python -m pytest -q",
        "status": "passed",
        "started_workspace_mutation_sequence": 7,
        "finished_workspace_mutation_sequence": 7,
        "started_changed_path_states": {},
        "finished_changed_path_states": {},
        "exit_code": 0,
        "output": "2 passed",
    }


def test_runtime_verification_classifies_sandbox_start_failure(tmp_path):
    class Sandbox:
        @staticmethod
        def run(*_args, **_kwargs):
            return SandboxResult(
                returncode=125,
                stderr="invalid mount config",
                infrastructure_error=True,
            )

    result = verify_workspace(
        root=tmp_path,
        command="python -m pytest -q",
        sandbox=Sandbox(),
        timeout_seconds=60,
        redact_text=str,
        mutation_sequence_provider=lambda: 7,
        started_workspace_mutation_sequence=7,
        changed_paths=(),
    )

    assert result["status"] == "infrastructure_error"
    assert result["exit_code"] == 125
    assert "invalid mount config" in result["output"]


def test_runtime_verification_records_mutation_cursor_drift(
    tmp_path,
):
    mutation_sequence = [7]

    class Sandbox:
        @staticmethod
        def run(*_args, **_kwargs):
            mutation_sequence[0] = 9
            return SandboxResult(returncode=0, stdout="2 passed")

    result = verify_workspace(
        root=tmp_path,
        command="python -m pytest -q",
        sandbox=Sandbox(),
        timeout_seconds=60,
        redact_text=str,
        mutation_sequence_provider=lambda: mutation_sequence[0],
        started_workspace_mutation_sequence=7,
        changed_paths=(),
    )

    assert result["status"] == "passed"
    assert "freshness" not in result
    assert not verification_is_current(result, 9, {})
    assert result["started_workspace_mutation_sequence"] == 7
    assert result["finished_workspace_mutation_sequence"] == 9


def test_runtime_verification_records_changed_path_drift(
    tmp_path,
):
    target = tmp_path / "subject.txt"
    target.write_text("before\n", encoding="utf-8")
    before = file_revision(target)

    class Sandbox:
        @staticmethod
        def run(*_args, **_kwargs):
            target.write_text("external\n", encoding="utf-8")
            return SandboxResult(returncode=0, stdout="2 passed")

    result = verify_workspace(
        root=tmp_path,
        command="python -m pytest -q",
        sandbox=Sandbox(),
        timeout_seconds=60,
        redact_text=str,
        mutation_sequence_provider=lambda: 7,
        started_workspace_mutation_sequence=7,
        changed_paths=("subject.txt",),
    )

    assert result["status"] == "passed"
    assert "freshness" not in result
    assert result["started_changed_path_states"] == {"subject.txt": before}
    assert result["finished_changed_path_states"] == {
        "subject.txt": file_revision(target)
    }
    assert not verification_is_current(
        result,
        7,
        {"subject.txt": file_revision(target)},
    )
