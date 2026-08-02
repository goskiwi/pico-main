"""命令行入口。

这个模块负责把"用户怎么启动 pico"翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import textwrap

from .config import (
    CONTEXT_COMPACTION_INPUT_TOKENS,
    CONTEXT_COMPACTION_MAX_PER_TASK,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MAX_STEPS,
)
from .models import OpenAICompatibleModelClient
from .repo_map import RepoMap
from .run_undo import RunUndoConflictError, RunUndoError, restore_run
from .runtime import Pico
from .sandbox import (
    DEFAULT_SANDBOX_CPUS,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SANDBOX_MEMORY,
    DEFAULT_SANDBOX_PIDS_LIMIT,
    DockerSandbox,
    DockerSandboxConfig,
)
from .session_store import SessionStore
from .agent.trace import TraceSink
from .workspace import WorkspaceContext, middle

# 配置日志：输出到 stderr，格式简洁，便于调试
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# 默认需要脱敏的环境变量名称列表
# 这些变量的值在日志和报告中会被隐藏
DEFAULT_SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "GITHUB_PAT",
    "GH_PAT",
)

# 欢迎界面 ASCII 艺术
WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "pico"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"

# 交互模式帮助信息
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /status  Show the model, session, prompt, and last-task status.
    /runs [limit] List recent main runs and their Undo availability.
    /memory  Show the agent's working state panel.
    /session Show the path to the saved session file.
    /reset   Clear the current session memory.
    /skill <name> Queue one trusted project skill for the next task.
    /reload-skills Reload trusted .pico/skills from disk.
    /exit    Exit the agent.
    """
).strip()


# 默认模型配置
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# 环境变量名称常量
SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"
ENV_LOCAL_FILENAME = ".env.local"
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_RUN_HISTORY_LIMIT = 10


def _positive_repo_map_budget(value):
    try:
        budget = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "repo map budget must be a positive integer"
        ) from exc
    if budget <= 0:
        raise argparse.ArgumentTypeError(
            "repo map budget must be a positive integer"
        )
    return budget


def _positive_run_history_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "run history limit must be a positive integer"
        ) from exc
    if limit <= 0:
        raise argparse.ArgumentTypeError(
            "run history limit must be a positive integer"
        )
    return limit


def _positive_repo_map_result_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "repo map result limit must be a positive integer"
        ) from exc
    if limit <= 0:
        raise argparse.ArgumentTypeError(
            "repo map result limit must be a positive integer"
        )
    return limit


def _load_workspace_env(cwd):
    """Read ``<cwd>/.env.local`` into a mapping for one Pico startup.

    Keep startup configuration narrow and auditable with this small
    dotenv-compatible reader rather than adding a general environment loader.
    The model client uses this mapping instead of ambient process variables.
    """
    env_path = Path(cwd).expanduser().resolve() / ENV_LOCAL_FILENAME
    if not env_path.is_file():
        return {}

    values = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not ENV_NAME_PATTERN.fullmatch(name):
            logger.warning("Ignoring invalid assignment in %s", env_path)
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    if values:
        logger.debug("Loaded %s variable(s) from %s", len(values), env_path)
    return values


def _effective_model(args, env=None):
    """Resolve the configured GPT-5.6-luna Responses model.

    模型选择优先级：
    1. 用户显式传入 --model
    2. `.env.local` 中的 `OPENAI_MODEL`
    3. 代码里的默认值
    """
    env = os.environ if env is None else env
    explicit_model = args.model
    if explicit_model:
        logger.info(f"使用用户指定的模型: {explicit_model}")
        return explicit_model
    model = env.get("OPENAI_MODEL")
    if model:
        logger.info(f"使用环境变量 OPENAI_MODEL: {model}")
        return model
    logger.info(f"使用默认模型: {DEFAULT_OPENAI_MODEL}")
    return DEFAULT_OPENAI_MODEL


def _configured_secret_names(args, env=None):
    """收集所有需要脱敏的环境变量名称。
    这些名称对应的值在日志和报告中会被隐藏，防止泄露敏感信息。
    """
    env = os.environ if env is None else env
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = env.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    logger.debug(f"配置的敏感环境变量: {sorted(configured_secret_names)}")
    return sorted(configured_secret_names)


def _build_model_client(args, env=None):
    """Build Pico's GPT-5.6-luna Responses client."""
    env = os.environ if env is None else env
    model = _effective_model(args, env=env)
    base_url = args.base_url or env.get("OPENAI_API_BASE") or DEFAULT_OPENAI_BASE_URL
    api_key = env.get("OPENAI_API_KEY", "")
    reasoning_effort = env.get("OPENAI_REASONING_EFFORT", "").strip() or None
    logger.info(
        "Responses API 配置 - model: %s, base_url: %s, reasoning_effort: %s",
        model,
        base_url,
        reasoning_effort or "provider default",
    )
    return OpenAICompatibleModelClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=args.temperature,
        timeout=args.model_timeout,
        reasoning_effort=reasoning_effort,
    )


def build_welcome(agent, model, host):
    """构建启动时显示的欢迎界面。

    包含 ASCII 艺术、工作区路径、模型信息、分支、审批策略等。
    """
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            pair("SANDBOX", agent.sandbox.backend, "IMAGE", agent.sandbox.config.image),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_status(agent):
    """Render the REPL state without changing the active session."""
    prompt_metadata = agent.last_prompt_metadata
    task_state = agent.current_task_state

    if prompt_metadata:
        prompt = (
            f"{int(prompt_metadata['prompt_tokens'])}/"
            f"{int(prompt_metadata['prompt_budget_tokens'])} tokens"
        )
        workspace_refresh = (
            "refreshed" if agent._last_prefix_refresh["workspace_changed"] else "reused"
        )
    else:
        prompt = "not built yet"
        workspace_refresh = "not evaluated yet"

    if task_state is None:
        last_task = "none"
    else:
        last_task = (
            f"{task_state.run_id} | {task_state.status} | "
            f"{len(task_state.context_compactions)} compaction(s)"
        )

    return "\n".join(
        (
            f"model: {agent.model_client.model}",
            f"session: {agent.session_path}",
            f"workspace refresh for last task: {workspace_refresh}",
            f"prompt: {prompt}",
            "task compaction policy: "
            f"{CONTEXT_COMPACTION_INPUT_TOKENS} tokens, "
            f"max {CONTEXT_COMPACTION_MAX_PER_TASK} per task",
            f"last task: {last_task}",
        )
    )


def _workspace_root_for_run_history(cwd):
    """Resolve the workspace that owns the requested, local run index."""
    explicit_workspace = Path(cwd).expanduser().resolve()
    if (explicit_workspace / ".pico" / "runs").is_dir():
        return explicit_workspace
    return Path(WorkspaceContext.build(explicit_workspace).repo_root)


def load_run_history(workspace_root, *, limit=DEFAULT_RUN_HISTORY_LIMIT):
    """Load current-schema main-run summaries without creating runtime files."""
    runs_root = Path(workspace_root) / ".pico" / "runs"
    index_path = runs_root / "index.json"
    if not index_path.is_file():
        return []
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(index, list):
        return []

    main_entries = [
        entry
        for entry in index
        if isinstance(entry, dict)
        and entry.get("agent_mode") == "main"
        and entry.get("parent_agent_id") == ""
    ]
    main_entries.sort(key=lambda entry: str(entry.get("updated_at", "")), reverse=True)

    history = []
    for entry in main_entries[:limit]:
        run_id = str(entry.get("run_id", ""))
        if not run_id or Path(run_id).name != run_id:
            continue
        report_path = runs_root / run_id / "report.json"
        report = {}
        if report_path.is_file():
            try:
                loaded_report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded_report = {}
            if isinstance(loaded_report, dict):
                report = loaded_report
        undo = report.get("undo", {})
        undo = undo if isinstance(undo, dict) else {}
        history.append(
            {
                "run_id": run_id,
                "status": str(entry.get("status", "unknown")),
                "updated_at": str(entry.get("updated_at", "")),
                "undo": (
                    "available"
                    if undo.get("available") is True
                    else str(undo.get("status", "pending"))
                ),
                "changed_paths": [
                    str(path)
                    for path in undo.get("changed_paths", [])
                    if isinstance(path, str) and path
                ],
            }
        )
    return history


def build_run_history(workspace_root, *, limit=DEFAULT_RUN_HISTORY_LIMIT):
    """Render a compact, read-only main-run history for CLI and REPL users."""
    workspace_root = Path(workspace_root).resolve()
    lines = [f"Pico runs: {workspace_root}"]
    history = load_run_history(workspace_root, limit=limit)
    if not history:
        lines.append("no main runs recorded")
        return "\n".join(lines)

    for entry in history:
        changed_paths = entry["changed_paths"]
        if not changed_paths:
            changed = "-"
        elif len(changed_paths) <= 3:
            changed = ", ".join(changed_paths)
        else:
            changed = ", ".join(changed_paths[:3]) + f" (+{len(changed_paths) - 3})"
        lines.extend(
            (
                f"{entry['run_id']} | {entry['status']} | {entry['updated_at']}",
                f"  undo: {entry['undo']} | changed: {changed}",
            )
        )
    return "\n".join(lines)


def build_repo_map_output(workspace_root, query, *, budget_tokens, max_results):
    """Render the same task-ranked map used by the agent without a model call."""
    workspace_root = Path(workspace_root).resolve()
    rendered = RepoMap(workspace_root).render(
        str(query),
        budget_tokens=int(budget_tokens),
        max_results=int(max_results),
    )
    details = rendered.details
    lines = [rendered.text, "", "repo_map_stats:"]
    lines.append(f"- workspace: {workspace_root}")
    lines.append(
        "- files={parsed_files} nodes={graph_nodes} edges={graph_edges} "
        "selected={selected_count} cache_hits={cache_hits} cache_misses={cache_misses}".format(
            **details
        )
    )
    selected = list(details.get("selected_symbols", []))
    if selected:
        lines.append("rank_evidence:")
        for item in selected:
            reasons = ", ".join(item.get("reasons", [])) or "-"
            lines.append(
                "- {path}:L{line} {qualified_name} "
                "score={score} lexical={lexical_score} graph={graph_score} "
                "reasons={reasons}".format(**{**item, "reasons": reasons})
            )
    return "\n".join(lines).strip()


def build_agent(args, *, trace_sink=None):
    """根据 CLI 参数装配出一个可运行的 Pico 实例。
    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pico`，或一个从旧 session 恢复出来的 `Pico`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先整理 secret 名单，再采集工作区快照，随后决定是恢复旧 session
    # 还是创建一个新的 Pico 实例。
    logger.info("开始构建 Pico agent...")

    # 模型配置只来自工作区 .env.local，避免隐式使用 shell、CI 或
    # secret manager 中碰巧存在的其他项目凭据。配置始终作为显式 mapping
    # 传给装配函数，不能写入进程环境，否则项目文件可污染父进程的 ambient env。
    workspace_env = _load_workspace_env(args.cwd)

    # 收集需要脱敏的环境变量名称
    configured_secret_names = _configured_secret_names(args, env=workspace_env)
    logger.debug(f"敏感环境变量数量: {len(configured_secret_names)}")

    # 构建工作区上下文，包含文件快照和 git 状态
    workspace = WorkspaceContext.build(
        args.cwd,
        verification_command=args.verify_cmd,
    )
    logger.info(f"工作区路径: {workspace.cwd}, 分支: {workspace.branch}")

    # 初始化 session 存储器
    store = SessionStore(workspace.repo_root + "/.pico/sessions")
    logger.debug(f"Session 存储路径: {workspace.repo_root}/.pico/sessions")

    # 构建模型客户端
    model = _build_model_client(args, env=workspace_env)
    sandbox_config = DockerSandboxConfig(
        image=args.sandbox_image
        or os.environ.get("PICO_SANDBOX_IMAGE")
        or DEFAULT_SANDBOX_IMAGE,
        cpus=float(args.sandbox_cpus),
        memory=str(args.sandbox_memory),
        pids_limit=int(args.sandbox_pids_limit),
    )
    sandbox = DockerSandbox(workspace.repo_root, config=sandbox_config)

    # 判断是恢复旧 session 还是创建新 session
    session_id = args.resume
    dry_run = bool(args.dry_run)

    if session_id == "latest":
        session_id = store.latest()
        if session_id:
            logger.info(f"恢复最新的 session: {session_id}")
        else:
            logger.info("没有找到可恢复的 session，将创建新的")

    if session_id:
        logger.info(f"从 session 恢复: {session_id}")
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            repo_map_budget_tokens=args.repo_map_budget,
            dry_run=dry_run,
            trust_project=args.trust_project,
            secret_env_names=configured_secret_names,
            sandbox=sandbox,
            trace_sink=trace_sink,
        )

    logger.info("创建新的 Pico 实例")
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        repo_map_budget_tokens=args.repo_map_budget,
        dry_run=dry_run,
        trust_project=args.trust_project,
        secret_env_names=configured_secret_names,
        sandbox=sandbox,
        trace_sink=trace_sink,
    )


def build_arg_parser():
    """构建命令行参数解析器。

    定义所有支持的命令行参数，包括：
    - prompt: one-shot 模式的提示词
    - --cwd: 工作区目录
    - --model: 模型名称
    - --base-url: Responses API 地址
    - --approval: 审批策略
    - --dry-run: 模拟模式
    等
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Auditable, sandboxed local coding-agent runtime for "
            "GPT-5.6-luna through an OpenAI-compatible Responses API."
        ),
        epilog=(
            "Restore one run with: "
            "pico undo --cwd /path/to/workspace --run <run_id>"
        ),
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model override. Defaults to OPENAI_MODEL in .env.local.",
    )
    parser.add_argument("--base-url", default=None, help="Responses API base URL override.")
    parser.add_argument("--model-timeout", type=int, default=300, help="Model request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument(
        "--trust-project",
        action="store_true",
        help=(
            "Allow discovery of project-local .pico/skills. Disabled by default "
            "because skills can become model instructions."
        ),
    )
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default=DEFAULT_APPROVAL_POLICY,
        help="Approval policy for risky tools.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate risky tools without running shell commands or writing files.")
    parser.add_argument(
        "--sandbox-image",
        default=os.environ.get("PICO_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE),
        help="Prebuilt Docker image used for every run_shell call.",
    )
    parser.add_argument(
        "--sandbox-cpus",
        type=float,
        default=DEFAULT_SANDBOX_CPUS,
        help="Maximum CPUs available to a shell sandbox.",
    )
    parser.add_argument(
        "--sandbox-memory",
        default=DEFAULT_SANDBOX_MEMORY,
        help="Docker memory limit for a shell sandbox, for example 512m.",
    )
    parser.add_argument(
        "--sandbox-pids-limit",
        type=int,
        default=DEFAULT_SANDBOX_PIDS_LIMIT,
        help="Maximum process count available to a shell sandbox.",
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Maximum tool calls per request.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument(
        "--verify-cmd",
        default="",
        help=(
            "Explicit command run by Pico in the Docker sandbox before accepting a changed task. "
            "A failed command grants one repair attempt; omit to disable runtime verification."
        ),
    )
    parser.add_argument(
        "--repo-map-budget",
        type=_positive_repo_map_budget,
        default=None,
        help=(
            "Hard token cap for the automatically injected Repo Map section. "
            "Omit to keep dynamic budgeting."
        ),
    )
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to the model.")
    trace_group = parser.add_mutually_exclusive_group()
    trace_group.add_argument(
        "--trace",
        action="store_true",
        help="Print concise live trace events to stderr.",
    )
    trace_group.add_argument(
        "--trace-jsonl",
        metavar="PATH",
        help="Mirror live JSONL events to PATH, or '-' for stdout in one-shot mode.",
    )
    return parser


def build_trace_sink(args):
    """Build the optional live trace mirror; every run still persists its JSONL artifact."""
    if bool(args.trace):
        return TraceSink("terminal", sys.stderr)
    raw_target = str(args.trace_jsonl or "").strip()
    if not raw_target:
        return None
    if raw_target == "-":
        return TraceSink("jsonl", sys.stdout)
    path = Path(raw_target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return TraceSink("jsonl", path.open("w", encoding="utf-8"), close_stream=True)


def build_undo_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pico undo",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Restore the workspace preimages recorded for one Pico run. "
            "The command refuses the entire undo if any affected path "
            "changed after that run."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Workspace directory or a path inside its Git repository.",
    )
    parser.add_argument(
        "--run",
        required=True,
        dest="run_id",
        help="Run id whose recorded workspace changes should be restored.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate conflicts and list paths without changing files.",
    )
    return parser


def build_runs_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pico runs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "List recent main-task run artifacts and whether their workspace "
            "changes remain eligible for Undo."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Workspace directory or a path inside its Git repository.",
    )
    parser.add_argument(
        "--limit",
        type=_positive_run_history_limit,
        default=DEFAULT_RUN_HISTORY_LIMIT,
        help="Maximum number of main runs to display.",
    )
    return parser


def build_repo_map_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pico repo-map",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Render Pico's task-ranked Python Repo Map without constructing an "
            "agent, calling a model, or writing run artifacts."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Workspace directory or a path inside its Git repository.",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Task or code-navigation question used to rank symbols.",
    )
    parser.add_argument(
        "--budget-tokens",
        type=_positive_repo_map_budget,
        default=1200,
        help="Hard token budget for rendered signatures.",
    )
    parser.add_argument(
        "--max-results",
        type=_positive_repo_map_result_limit,
        default=24,
        help="Maximum number of ranked symbols to render.",
    )
    return parser


def run_repo_map_command(argv):
    args = build_repo_map_arg_parser().parse_args(argv)
    workspace_root = Path(WorkspaceContext.build(args.cwd).repo_root)
    print(
        build_repo_map_output(
            workspace_root,
            args.query,
            budget_tokens=args.budget_tokens,
            max_results=args.max_results,
        )
    )
    return 0


def run_runs_command(argv):
    args = build_runs_arg_parser().parse_args(argv)
    workspace_root = _workspace_root_for_run_history(args.cwd)
    print(build_run_history(workspace_root, limit=args.limit))
    return 0


def run_undo_command(argv):
    args = build_undo_arg_parser().parse_args(argv)
    explicit_workspace = Path(args.cwd).resolve()
    # A benchmark fixture can deliberately omit ``.git`` so an Agent cannot
    # inspect history.  In that case WorkspaceContext would walk upward into
    # the host repository, even though the explicit cwd owns its own Pico run
    # store.  Prefer that direct run store; retain the Git-root fallback for
    # callers that pass a nested directory inside a normal repository.
    if (explicit_workspace / ".pico" / "runs").is_dir():
        workspace_root = explicit_workspace
    else:
        workspace_root = Path(WorkspaceContext.build(args.cwd).repo_root)
    try:
        result = restore_run(
            workspace_root,
            args.run_id,
            dry_run=args.dry_run,
        )
    except RunUndoConflictError as exc:
        print(
            "undo refused: workspace paths changed after the run",
            file=sys.stderr,
        )
        for path in exc.paths:
            print(f"- {path}", file=sys.stderr)
        return 1
    except RunUndoError as exc:
        print(f"undo failed: {exc}", file=sys.stderr)
        return 1

    if result.already_restored:
        print(f"run already restored: {result.run_id}")
        return 0
    action = "would restore" if result.dry_run else "restored"
    print(
        f"{action} {len(result.restored_paths)} path(s) "
        f"for run {result.run_id}"
    )
    for path in result.restored_paths:
        marker = "delete" if path in result.deleted_paths else "restore"
        print(f"- {marker}: {path}")
    return 0


def main(argv=None):
    """命令行主入口函数。
    解析参数 -> 构建 agent -> 显示欢迎信息 -> 进入 one-shot 或交互模式。
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "repo-map":
        return run_repo_map_command(raw_argv[1:])
    if raw_argv and raw_argv[0] == "runs":
        return run_runs_command(raw_argv[1:])
    if raw_argv and raw_argv[0] == "undo":
        return run_undo_command(raw_argv[1:])
    parser = build_arg_parser()
    args = parser.parse_args(raw_argv)
    if args.trace_jsonl == "-" and not args.prompt:
        parser.error("--trace-jsonl - requires a one-shot prompt")
    # Parse first so ``pico --help`` remains a clean, side-effect-free CLI
    # surface instead of printing startup logs before argparse exits.
    trace_sink = build_trace_sink(args)
    output_stream = sys.stderr if args.trace_jsonl == "-" else sys.stdout
    try:
        logger.info("pico 启动中...")
        logger.debug(f"命令行参数: {args}")

        agent = build_agent(args, trace_sink=trace_sink)
        logger.info("Pico agent 构建完成")

        model = agent.model_client.model
        base_url = agent.model_client.base_url
        print(build_welcome(agent, model=model, host=base_url), file=output_stream)

        if args.prompt:
            # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
            prompt = " ".join(args.prompt).strip()
            if not prompt:
                return 0
            logger.info(f"one-shot 模式，提示词长度: {len(prompt)} 字符")
            print(file=output_stream)
            try:
                result = agent.ask(prompt)
                print(result, file=output_stream)
                logger.info("one-shot 执行完成")
            except RuntimeError as exc:
                logger.error(f"执行出错: {exc}")
                print(str(exc), file=sys.stderr)
                return 1
            return 0

        logger.info("进入交互模式")
        while True:
            # 交互模式：每次读取一条用户输入，交给同一个 agent，
            # 因此 working memory 和 checkpoint 会跨轮延续。
            try:
                user_input = input("\npico> ").strip()
            except (EOFError, KeyboardInterrupt):
                logger.info("用户中断，退出程序")
                print("")
                return 0

            if not user_input:
                continue
            if user_input in {"/exit", "/quit"}:
                logger.info("用户请求退出")
                return 0
            if user_input == "/help":
                print(HELP_DETAILS)
                continue
            if user_input == "/status":
                print(build_status(agent))
                continue
            if user_input == "/runs" or user_input.startswith("/runs "):
                _, _, raw_limit = user_input.partition(" ")
                try:
                    limit = (
                        _positive_run_history_limit(raw_limit)
                        if raw_limit.strip()
                        else DEFAULT_RUN_HISTORY_LIMIT
                    )
                except argparse.ArgumentTypeError as exc:
                    print(f"runs not shown: {exc}")
                else:
                    print(build_run_history(agent.workspace.repo_root, limit=limit))
                continue
            if user_input == "/memory":
                logger.debug("显示 memory")
                print(agent.memory_text())
                continue
            if user_input == "/session":
                logger.debug(f"显示 session 路径: {agent.session_path}")
                print(agent.session_path)
                continue
            if user_input == "/reset":
                logger.info("重置 session")
                agent.reset()
                print("session reset")
                continue
            if user_input == "/reload-skills":
                logger.info("重新加载 skills")
                skills = agent.reload_skills()
                print(f"skills reloaded: {len(skills)}")
                if not agent.trust_project:
                    print("project skills are disabled; restart with --trust-project to load .pico/skills")
                for diagnostic in agent.skill_diagnostics:
                    print(f"skill warning: {diagnostic['path']}: {diagnostic['message']}")
                continue
            if user_input.startswith("/skill"):
                command, _, name = user_input.partition(" ")
                if command != "/skill":
                    print("unknown command; use /help")
                    continue
                try:
                    skill = agent.queue_manual_skill(name)
                except ValueError as exc:
                    print(f"skill not queued: {exc}")
                else:
                    print(f"skill queued for next task: {skill.name}")
                continue

            print()
            try:
                logger.debug(f"处理用户输入: {user_input[:50]}...")
                result = agent.ask(user_input)
                print(result)
            except RuntimeError as exc:
                logger.error(f"执行出错: {exc}")
                print(str(exc), file=sys.stderr)
    finally:
        if trace_sink is not None:
            trace_sink.close()
