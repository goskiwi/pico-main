from pico.evaluation.provenance import evaluation_snapshot_id, runtime_snapshot_id


def test_runtime_snapshot_is_stable_and_content_bound(tmp_path):
    (tmp_path / "pico").mkdir()
    (tmp_path / "pico" / "runtime.py").write_text("VALUE = 1\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_evaluations.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    first = runtime_snapshot_id(tmp_path)
    assert first == runtime_snapshot_id(tmp_path)
    (tmp_path / "pico" / "runtime.py").write_text("VALUE = 2\n")
    assert runtime_snapshot_id(tmp_path) != first


def test_evaluation_snapshot_is_separate_from_runtime_source(tmp_path):
    (tmp_path / "pico" / "evaluation").mkdir(parents=True)
    (tmp_path / "pico" / "runtime.py").write_text("VALUE = 1\n")
    (tmp_path / "pico" / "evaluation" / "evaluator.py").write_text("VALUE = 1\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_evaluations.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    before_runtime = runtime_snapshot_id(tmp_path)
    before_evaluation = evaluation_snapshot_id(tmp_path)

    (tmp_path / "pico" / "evaluation" / "evaluator.py").write_text("VALUE = 2\n")

    assert runtime_snapshot_id(tmp_path) == before_runtime
    assert evaluation_snapshot_id(tmp_path) != before_evaluation


def test_runtime_snapshot_ignores_bytecode_cache(tmp_path):
    (tmp_path / "pico" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pico" / "runtime.py").write_text("VALUE = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    before = runtime_snapshot_id(tmp_path)

    (tmp_path / "pico" / "__pycache__" / "runtime.cpython-314.pyc").write_bytes(b"cache")

    assert runtime_snapshot_id(tmp_path) == before
