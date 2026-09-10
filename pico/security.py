"""Security and redaction helpers for runtime values."""

import os

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"
EXTRA_SECRET_ENV_NAMES = "PICO_SECRET_ENV_NAMES"
DEFAULT_SECRET_ENV_NAMES = frozenset({
    "PICO_OPENAI_API_KEY", "OPENAI_API_KEY", "OPENAI_API_TOKEN",
    "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "GITHUB_PAT", "GH_PAT",
})


def configured_secret_env_names(env=None):
    env = os.environ if env is None else env
    return DEFAULT_SECRET_ENV_NAMES | {
        name.strip().upper()
        for name in str(env.get(EXTRA_SECRET_ENV_NAMES, "")).split(",")
        if name.strip()
    }


def looks_sensitive_env_name(name):
    upper = str(name).upper()
    return any(upper.endswith(marker) for marker in SENSITIVE_ENV_NAME_MARKERS)


def is_secret_env_name(name, env=None):
    upper = str(name).upper()
    return upper in configured_secret_env_names(env) or looks_sensitive_env_name(upper)


def detected_secret_env_items(env=None):
    env = os.environ if env is None else env
    items = [
        (name, value)
        for name, value in env.items()
        if is_secret_env_name(name, env=env) and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def redact_text(text, env=None):
    text = str(text)
    for _, value in sorted(
        detected_secret_env_items(env=env),
        key=lambda item: len(item[1]),
        reverse=True,
    ):
        text = text.replace(value, REDACTED_VALUE)
    return text


def redact_facts(value, redactor, key=""):
    # These values are machine identities consumed by replay and mutation logic.
    if key in {
        "path", "affected_paths", "changed_paths", "repository_changes", "revision",
        "expected_revision", "actual_revision", "before_revision", "after_revision",
        "before_state", "after_state", "before_artifact_id", "artifact_id",
        "sha256", "status",
        "started_changed_path_states", "finished_changed_path_states",
        "workspace_root", "workspace_changes", "run_id", "session_id",
    }:
        return value
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, dict):
        return {
            str(item_key): redact_facts(item, redactor, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_facts(item, redactor, key) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redactor(str(value))
