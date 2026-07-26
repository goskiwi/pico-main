import importlib.util
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "materialize_real_oss_v1.py"
MANIFEST_PATH = PROJECT_ROOT / "benchmarks" / "real_oss_v1.json"


def _load_materializer():
    spec = importlib.util.spec_from_file_location("materialize_real_oss_v1", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(args, *, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_real_oss_manifest_has_three_unique_frozen_source_tasks():
    materializer = _load_materializer()

    manifest = materializer.load_manifest(MANIFEST_PATH)

    assert [task["id"] for task in manifest["tasks"]] == [
        "pydantic_slots_forwarded_generic",
        "pytest_indirect_parametrize",
        "click_empty_bytes_echo",
    ]
    assert all(task["fixture_repo"].startswith("artifacts/real-oss-fixtures/") for task in manifest["tasks"])


def test_pytest_task_requires_source_local_verification():
    materializer = _load_materializer()

    manifest = materializer.load_manifest(MANIFEST_PATH)
    task = next(task for task in manifest["tasks"] if task["id"] == "pytest_indirect_parametrize")

    assert "PYTHONPATH=src python -m pytest" in task["prompt"]
    assert "focused regression test" in task["prompt"]


def test_materialize_task_fetches_one_commit_and_strips_git_metadata(tmp_path):
    materializer = _load_materializer()
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "--quiet"], cwd=source)
    _git(["config", "user.email", "test@example.com"], cwd=source)
    _git(["config", "user.name", "Test User"], cwd=source)
    (source / "package").mkdir()
    (source / "package" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(["add", "."], cwd=source)
    _git(["commit", "--quiet", "-m", "initial"], cwd=source)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    task = {
        "id": "fixture",
        "fixture_repo": "artifacts/real-oss-fixtures/fixture",
        "source_repository": str(source),
        "source_commit": commit,
        "expected_files": ["package/module.py"],
        "generated_files": {"package/generated.py": "GENERATED = True\n"},
    }

    record = materializer.materialize_task(tmp_path, task, replace=False)

    fixture = tmp_path / task["fixture_repo"]
    assert record["source_commit"] == commit
    assert record["tree_digest"].startswith("sha256:")
    assert (fixture / "package" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (fixture / "package" / "generated.py").read_text(encoding="utf-8") == "GENERATED = True\n"
    assert not (fixture / ".git").exists()
