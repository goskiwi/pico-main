"""集中配置：所有模块共享的常量和默认值。

以前这些数字散落在各个模块里，改一个默认值要翻好几个文件。
现在统一收到这里，方便调参和测试覆盖。
"""

from __future__ import annotations

from typing import Tuple


# ---------------------------------------------------------------------------
# 运行时默认值
# ---------------------------------------------------------------------------

DEFAULT_MAX_STEPS: int = 6
DEFAULT_MAX_NEW_TOKENS: int = 512
DEFAULT_MAX_DEPTH: int = 1
DEFAULT_APPROVAL_POLICY: str = "ask"
DEFAULT_RUNTIME_VERIFICATION_TIMEOUT_SECONDS: int = 90
MEMORY_EXTRACTOR_MAX_TOKENS: int = 512

# 委派调度护栏。一个父 agent 最多同时运行三个只读子 agent，所有子 agent
# 预留的步骤总数不能超过 12；超过预算的任务会得到明确结果而不是静默丢失。
DELEGATE_MAX_CONCURRENCY: int = 3
DELEGATE_TOTAL_STEP_BUDGET: int = 12
DELEGATE_BATCH_TIMEOUT_SECONDS: float = 180.0

DEFAULT_FEATURE_FLAGS: dict = {
    "memory": True,
    "relevant_memory": True,
    "repo_map": True,
    "durable_memory_promotion": True,
    "context_reduction": True,
    "prompt_cache": True,
    "llm_memory_extract": True,
    "dynamic_budget": True,
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
# 上下文预算（真实 tokenizer token 数）
# ---------------------------------------------------------------------------

DEFAULT_TOTAL_BUDGET: int = 12000
DEFAULT_SECTION_BUDGETS: dict = {
    "prefix": 3000,
    "memory": 1200,
    "skills": 900,
    "repo_map": 1800,
    "relevant_memory": 900,
    "task_context": 4200,
}
DEFAULT_REDUCTION_ORDER: Tuple[str, ...] = (
    "relevant_memory",
    "skills",
    "task_context",
    "repo_map",
    "memory",
    "prefix",
)
RELEVANT_MEMORY_LIMIT: int = 3

# The Mermaid canvas is an audit/recovery control plane, not the live tool
# conversation.  Fold older completed steps into a drill-down phase artifact
# before the UI view becomes hard to navigate.
TASK_CANVAS_MAX_ACTIVE_NODES: int = 12
TASK_CANVAS_RETAIN_NODES: int = 8
TASK_CANVAS_MAX_TOKENS: int = 2800

# A provider-side action conversation normally keeps exact tool results.  If
# the provider explicitly rejects that conversation for context overflow, one
# fresh session is recovered from this bounded, tokenizer-measured evidence
# bundle.  A second overflow stops transparently instead of repeatedly
# summarizing and silently losing details.
CONTEXT_RECOVERY_MAX_TOOL_OUTPUTS: int = 5
CONTEXT_RECOVERY_MAX_FILES: int = 4
CONTEXT_RECOVERY_EVIDENCE_TOKENS: int = 3600

# ---------------------------------------------------------------------------
# Task-aware Python repository map
# ---------------------------------------------------------------------------

REPO_MAP_MAX_FILES: int = 2000
REPO_MAP_MAX_FILE_BYTES: int = 512_000
REPO_MAP_PAGE_RANK_ITERATIONS: int = 32
REPO_MAP_DAMPING: float = 0.85

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
