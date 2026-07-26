import importlib.util
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "materialize_real_oss_v1.py"
MANIFEST_PATH = PROJECT_ROOT / "benchmarks" / "real_oss_v1.json"
V2_MANIFEST_PATH = PROJECT_ROOT / "benchmarks" / "real_oss_v2.json"


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


def test_real_oss_v2_freezes_ten_distinct_upstream_repositories():
    materializer = _load_materializer()

    manifest = materializer.load_manifest(V2_MANIFEST_PATH)
    tasks = manifest["tasks"]

    assert manifest["name"] == "pico-real-oss-v2"
    assert [task["id"] for task in tasks] == [
        "pydantic_slots_forwarded_generic",
        "pytest_indirect_parametrize",
        "click_empty_bytes_echo",
        "tomlkit_float_not_sequence",
        "tqdm_infinite_total_format_meter",
        "packaging_non_string_version",
        "werkzeug_float_url_notation",
        "more_itertools_negative_tail",
        "jinja_overlay_async_default",
        "urllib3_port_zero",
    ]
    assert len({task["source_repository"] for task in tasks}) == 10
    assert all(task["fixture_repo"].startswith("artifacts/real-oss-fixtures/") for task in tasks)
    assert tasks[-1]["generated_files"] == {
        "src/urllib3/_version.py": '__version__ = "2.5.0.dev0"\n'
    }


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


def test_materialization_sidecar_is_namespaced_by_manifest(tmp_path, monkeypatch):
    materializer = _load_materializer()
    commit = "a" * 40

    manifest = tmp_path / "real_oss_v2.json"
    manifest.write_text(
        """{
  \"schema_version\": 1,
  \"tasks\": [{
    \"id\": \"fixture\",
    \"fixture_repo\": \"artifacts/real-oss-fixtures/fixture\",
    \"source_repository\": \"REPOSITORY\",
    \"source_commit\": \"COMMIT\",
    \"expected_files\": [\"module.py\"]
  }]
}
""".replace("REPOSITORY", "https://example.test/source.git").replace("COMMIT", commit),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        materializer,
        "materialize_task",
        lambda repo_root, task, *, replace: {
            "id": task["id"],
            "fixture_repo": task["fixture_repo"],
            "source_repository": task["source_repository"],
            "source_commit": task["source_commit"],
            "tree_digest": "sha256:fixture",
        },
    )

    materializer.materialize_manifest(manifest, repo_root=tmp_path)

    assert (tmp_path / "artifacts/real-oss-fixtures/.real_oss_v2.materialization.json").is_file()
    assert not (tmp_path / "artifacts/real-oss-fixtures/.materialization.json").exists()
