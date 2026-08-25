"""Small atomic file primitives used by Runtime snapshots and outputs."""

import json
import os
import tempfile
from pathlib import Path


def atomic_replace_bytes(path, payload, *, mode=0o600, commit_guard=None):
    """Stage complete bytes, validate the commit condition, then replace."""

    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=path.name + ".",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(int(mode))
        if commit_guard is not None:
            commit_guard()
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def write_once_bytes(path, payload, *, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(int(mode))
        return True
    except FileExistsError:
        return False


def atomic_write_json(path, payload):
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return atomic_replace_bytes(path, encoded)
