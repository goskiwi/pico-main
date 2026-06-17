"""集中配置：所有模块共享的常量和默认值。

以前这些数字散落在各个模块里，改一个默认值要翻好几个文件。
现在统一收到这里，方便调参和测试覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


# ---------------------------------------------------------------------------
# 运行时默认值
# ---------------------------------------------------------------------------

DEFAULT_MAX_STEPS: int = 6
DEFAULT_MAX_NEW_TOKENS: int = 512
DEFAULT_MAX_DEPTH: int = 1
DEFAULT_APPROVAL_POLICY: str = "ask"
MEMORY_EXTRACTOR_MAX_TOKENS: int = 512

DEFAULT_FEATURE_FLAGS: dict = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
    "llm_memory_extract": True,
    "llm_history_compaction": True,
}

DEFAULT_SHELL_ENV_ALLOWLIST: Tuple[str, ...] = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
)

SENSITIVE_ENV_NAME_MARKERS: Tuple[str, ...] = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE: str = "<redacted>"

# ---------------------------------------------------------------------------
# 上下文预算（token 估算值）
# ---------------------------------------------------------------------------

DEFAULT_TOTAL_BUDGET: int = 12000
DEFAULT_SECTION_BUDGETS: dict = {
    "prefix": 3600,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}
DEFAULT_SECTION_FLOORS: dict = {
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}
DEFAULT_REDUCTION_ORDER: Tuple[str, ...] = ("relevant_memory", "history", "memory", "prefix")
HISTORY_RECENT_WINDOW: int = 6
RELEVANT_MEMORY_LIMIT: int = 3
FILE_PRIORITY_LIMIT: int = 5
LLM_COMPACT_MAX_INPUT_CHARS: int = 12000
LLM_COMPACT_MAX_OUTPUT_TOKENS: int = 700

# ---------------------------------------------------------------------------
# 工作记忆 / 持久记忆
# ---------------------------------------------------------------------------

WORKING_FILE_LIMIT: int = 8
EPISODIC_NOTE_LIMIT: int = 12
FILE_SUMMARY_LIMIT: int = 6
MAX_MEMORY_INDEX_LINES: int = 200
MAX_MEMORY_INDEX_BYTES: int = 25_000
STALE_DURABLE_MEMORY_DAYS: int = 2

# ---------------------------------------------------------------------------
# 工作区 / 工具
# ---------------------------------------------------------------------------

MAX_TOOL_OUTPUT: int = 4000
MAX_HISTORY: int = 12000
DOC_NAMES: Tuple[str, ...] = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
IGNORED_PATH_NAMES: frozenset = frozenset(
    {".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}
)

PROTECTED_WRITE_PATH_PARTS: frozenset = frozenset(
    {".git", ".pico", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
)
PROTECTED_WRITE_FILENAMES: frozenset = frozenset({".env", ".env.local"})

ALLOWED_SHELL_COMMANDS: Tuple[Tuple[str, ...], ...] = (
    ("python", "-m", "pytest"),
    ("python", "-m", "compileall"),
    ("pytest",),
    ("ruff", "check"),
    ("uv", "run", "pytest"),
    ("uv", "run", "ruff", "check"),
    ("uv", "run", "python", "-m", "pytest"),
    ("uv", "run", "python", "-m", "compileall"),
)

# DANGEROUS_SHELL_PATTERNS 在这里定义但 re.compile 在 tools.py 里执行，
# 因为 config.py 不应该有重的 import 依赖。
_DANGEROUS_SHELL_PATTERNS_RAW: Tuple[Tuple[str, str], ...] = (
    ("recursive forced delete", r"(?i)(^|[;&|]\s*)rm\s+[^;&|]*-[^\s;&|]*r[^\s;&|]*f|(^|[;&|]\s*)rm\s+[^;&|]*-[^\s;&|]*f[^\s;&|]*r"),
    ("filesystem wipe target", r"(?i)\brm\s+[^;&|]*(/|~|\.\.)\s*(?:[;&|]|$)"),
    ("hard git reset", r"(?i)\bgit\s+reset\s+--hard\b"),
    ("forced git clean", r"(?i)\bgit\s+clean\b[^;&|]*\s-[^\s;&|]*f"),
    ("curl pipe shell", r"(?i)\b(curl|wget)\b[^;&|]*(\||>\s*/tmp/)[^;&|]*\b(sh|bash|zsh|python|perl|ruby)\b"),
    ("world-writable chmod", r"(?i)\bchmod\s+[^;&|]*(777|a\+w|ugo\+w)\b"),
    ("disk overwrite", r"(?i)\bdd\s+[^;&|]*\bof=/dev/"),
)

# ---------------------------------------------------------------------------
# 模型调用重试
# ---------------------------------------------------------------------------

DEFAULT_MODEL_MAX_RETRIES: int = 2
DEFAULT_MODEL_RETRY_BACKOFF: float = 1.0

# ---------------------------------------------------------------------------
# Session 存储
# ---------------------------------------------------------------------------

SESSION_COMPACT_INTERVAL: int = 50  # 每 N 次 save 触发一次 compaction


@dataclass
class PicoConfig:
    """运行时可调配置。

    零外部依赖——只用标准库 dataclass。
    所有字段都有默认值，所以 PicoConfig() 就能直接用。
    """

    # 运行时
    max_steps: int = DEFAULT_MAX_STEPS
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS
    max_depth: int = DEFAULT_MAX_DEPTH
    approval_policy: str = DEFAULT_APPROVAL_POLICY
    memory_extractor_max_tokens: int = MEMORY_EXTRACTOR_MAX_TOKENS
    feature_flags: dict = field(default_factory=lambda: dict(DEFAULT_FEATURE_FLAGS))
    shell_env_allowlist: Tuple[str, ...] = DEFAULT_SHELL_ENV_ALLOWLIST

    # 上下文预算
    total_budget: int = DEFAULT_TOTAL_BUDGET
    section_budgets: dict = field(default_factory=lambda: dict(DEFAULT_SECTION_BUDGETS))
    section_floors: dict = field(default_factory=lambda: dict(DEFAULT_SECTION_FLOORS))
    reduction_order: Tuple[str, ...] = DEFAULT_REDUCTION_ORDER
    history_recent_window: int = HISTORY_RECENT_WINDOW
    relevant_memory_limit: int = RELEVANT_MEMORY_LIMIT
    file_priority_limit: int = FILE_PRIORITY_LIMIT
    llm_compact_max_input_chars: int = LLM_COMPACT_MAX_INPUT_CHARS
    llm_compact_max_output_tokens: int = LLM_COMPACT_MAX_OUTPUT_TOKENS

    # 工作记忆
    working_file_limit: int = WORKING_FILE_LIMIT
    episodic_note_limit: int = EPISODIC_NOTE_LIMIT
    file_summary_limit: int = FILE_SUMMARY_LIMIT

    # 模型重试
    model_max_retries: int = DEFAULT_MODEL_MAX_RETRIES
    model_retry_backoff: float = DEFAULT_MODEL_RETRY_BACKOFF

    # Session 存储
    session_compact_interval: int = SESSION_COMPACT_INTERVAL
