"""Stable source fingerprints for published Runtime evaluation artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SNAPSHOT_INPUTS = (
    Path("pyproject.toml"),
    Path("pico"),
)
EVALUATION_SNAPSHOT_INPUTS = (
    Path("pico/evaluation"),
    Path("scripts/run_evaluations.py"),
    Path("benchmarks/coding_tasks.json"),
    Path("tests/fixtures/bench_repo_patch"),
    Path("tests/fixtures/bench_repo_readme"),
)
SNAPSHOT_SUFFIXES = frozenset({".json", ".md", ".py", ".toml", ".txt"})


def _snapshot_id(root, inputs, *, exclude=()):
    root = Path(root).resolve()
    excluded = {(root / path).resolve() for path in exclude}
    paths = []
    for relative in inputs:
        candidate = root / relative
        if candidate.is_file():
            paths.append(candidate)
        elif candidate.is_dir():
            paths.extend(
                path
                for path in candidate.rglob("*")
                if path.is_file()
                and path.suffix in SNAPSHOT_SUFFIXES
                and "__pycache__" not in path.parts
            )
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        if any(path == item or item in path.parents for item in excluded):
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def runtime_snapshot_id(root=REPOSITORY_ROOT):
    return _snapshot_id(
        root,
        RUNTIME_SNAPSHOT_INPUTS,
        exclude=(Path("pico/evaluation"),),
    )


def evaluation_snapshot_id(root=REPOSITORY_ROOT):
    return _snapshot_id(root, EVALUATION_SNAPSHOT_INPUTS)
