"""命令行入口。

这个模块负责把"用户怎么启动 pico"翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import textwrap

from .config import DEFAULT_APPROVAL_POLICY
from .models import OpenAICompatibleModelClient
from .memory import LayeredMemory, default_memory_state
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
from .semantic_memory import SemanticMemoryConfig, SemanticMemoryIndex
from .session_store import SessionStore
from .trace_events import TraceSink
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
    "PICO_QDRANT_API_KEY",
    "PICO_EMBEDDINGS_API_KEY",
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
    /memory  Show the agent's working state panel.
    /session Show the path to the saved session file.
    /reset   Clear the current session memory.
    /reload-skills Reload .pico/skills from disk.
    /exit    Exit the agent.
    """
).strip()


# 默认模型配置
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# 环境变量名称常量
SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"
ENV_LOCAL_FILENAME = ".env.local"
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    """根据 provider 和参数确定最终使用的模型名称。

    模型选择优先级：
    1. 用户显式传入 --model
    2. `.env.local` 中的 OPENAI_MODEL
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
    logger.info(f"使用默认 OpenAI 模型: {DEFAULT_OPENAI_MODEL}")
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
    """Build Pico's sole OpenAI-compatible model client."""
    env = os.environ if env is None else env
    model = _effective_model(args, env=env)
    base_url = args.base_url or env.get("OPENAI_API_BASE") or DEFAULT_OPENAI_BASE_URL
    api_key = env.get("OPENAI_API_KEY", "")
    reasoning_effort = env.get("OPENAI_REASONING_EFFORT", "").strip() or None
    logger.info(
        f"OpenAI 客户端配置 - model: {model}, base_url: {base_url}, "
        f"reasoning_effort: {reasoning_effort or 'provider default'}"
    )
    return OpenAICompatibleModelClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=args.temperature,
        timeout=args.openai_timeout,
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
    semantic_memory_config = SemanticMemoryConfig.from_env(workspace_env)
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
            secret_env_names=configured_secret_names,
            sandbox=sandbox,
            semantic_memory_config=semantic_memory_config,
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
        secret_env_names=configured_secret_names,
        sandbox=sandbox,
        semantic_memory_config=semantic_memory_config,
        trace_sink=trace_sink,
    )


def build_arg_parser():
    """构建命令行参数解析器。

    定义所有支持的命令行参数，包括：
    - prompt: one-shot 模式的提示词
    - --cwd: 工作区目录
    - --model: 模型名称
    - --base-url: OpenAI-compatible API 地址
    - --approval: 审批策略
    - --dry-run: 模拟模式
    等
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Auditable, sandboxed local coding-agent runtime for "
            "OpenAI-compatible Responses models."
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
        help="Model name override. Defaults to OPENAI_MODEL from .env.local when set.",
    )
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible Responses API base URL.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
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
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
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


def build_memory_sync_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pico memory-sync",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Synchronize canonical durable Markdown memories to Qdrant.",
    )
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    return parser


def run_memory_sync_command(argv):
    args = build_memory_sync_arg_parser().parse_args(argv)
    workspace = WorkspaceContext.build(args.cwd)
    workspace_env = _load_workspace_env(args.cwd)
    config = SemanticMemoryConfig.from_env(workspace_env)
    if config is None:
        print("semantic memory is not configured; set PICO_QDRANT_* and PICO_EMBEDDINGS_* in .env.local", file=sys.stderr)
        return 1
    workspace_id = hashlib.sha256(workspace.repo_root.encode("utf-8")).hexdigest()
    index = SemanticMemoryIndex(config, workspace_id=workspace_id)
    try:
        memory = LayeredMemory(
            default_memory_state(),
            workspace_root=workspace.repo_root,
            semantic_index=index,
        )
        result = memory.sync_semantic_memory()
    finally:
        index.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


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
    if raw_argv and raw_argv[0] == "undo":
        return run_undo_command(raw_argv[1:])
    if raw_argv and raw_argv[0] == "memory-sync":
        return run_memory_sync_command(raw_argv[1:])
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
