"""Project-local configuration helpers."""

import os
import re
from pathlib import Path

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORKING_FILE_LIMIT = 8
FILE_SUMMARY_LIMIT = 6
REPO_MAP_MAX_FILES = 2000
REPO_MAP_MAX_FILE_BYTES = 512_000
REPO_MAP_SCAN_MAX_ENTRIES = 20_000
REPO_MAP_SCAN_TIMEOUT_SECONDS = 2.0
REPO_MAP_PAGE_RANK_ITERATIONS = 32
REPO_MAP_DAMPING = 0.85


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        raise ValueError(f"invalid .env line: {line}")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(value)


def find_project_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        for name in (".env.local", ".env"):
            env_path = path / name
            if env_path.is_file():
                return env_path
    return None


def load_project_env(start, override=True):
    selected = find_project_env(start)
    if selected is None:
        return {}
    env_paths = [selected]
    sibling = selected.with_name(".env")
    local = selected.with_name(".env.local")
    if sibling.is_file() and local.is_file():
        env_paths = [sibling, local]
    loaded = {}
    for env_path in env_paths:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(line)
            if parsed is None:
                continue
            name, value = parsed
            loaded[name] = value
            if override or name not in os.environ:
                os.environ[name] = value
    return loaded


def provider_env(name, default=""):
    return os.environ.get(name) or default
