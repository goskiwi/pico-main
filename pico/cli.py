"""命令行入口。

这个模块负责把"用户怎么启动 pico"翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import logging
import os
import shutil
import sys
import textwrap

from .models import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import Pico
from .session_store import SessionStore
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
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "RIGHT_CODES_API_KEY",
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
    /reset   Clear the current session history and memory.
    /exit    Exit the agent.
    """
).strip()


# 默认模型配置
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"

# 环境变量名称常量
LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def _effective_model(args, provider):
    """根据 provider 和参数确定最终使用的模型名称。

    模型选择优先级：
    1. 用户显式传入 --model
    2. provider 对应的环境变量（如 OPENAI_MODEL）
    3. 代码里的默认值
    """
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        logger.info(f"使用用户指定的模型: {explicit_model}")
        return explicit_model
    if provider == "openai":
        model = os.environ.get("OPENAI_MODEL")
        if model:
            logger.info(f"使用环境变量 OPENAI_MODEL: {model}")
            return model
        logger.info(f"使用默认 OpenAI 模型: {DEFAULT_OPENAI_MODEL}")
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL")
        if model:
            logger.info(f"使用环境变量 ANTHROPIC_MODEL: {model}")
            return model
        logger.info(f"使用默认 Anthropic 模型: {DEFAULT_ANTHROPIC_MODEL}")
        return DEFAULT_ANTHROPIC_MODEL
    logger.info(f"使用默认 Ollama 模型: {DEFAULT_OLLAMA_MODEL}")
    return DEFAULT_OLLAMA_MODEL


def _first_env(*names):
    """按顺序查找环境变量，返回第一个非空值。"""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _configured_secret_names(args):
    """收集所有需要脱敏的环境变量名称。
    这些名称对应的值在日志和报告中会被隐藏，防止泄露敏感信息。
    """
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if not extra_names.strip():
        extra_names = os.environ.get(LEGACY_SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    logger.debug(f"配置的敏感环境变量: {sorted(configured_secret_names)}")
    return sorted(configured_secret_names)


def _build_model_client(args):
    """根据 provider 参数构建对应的模型客户端。

    CLI 只负责把 provider 选择翻译成具体 client。
    真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    """
    provider = getattr(args, "provider", "openai")
    logger.info(f"构建模型客户端，provider: {provider}")

    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or os.environ.get("OPENAI_API_BASE") or DEFAULT_OPENAI_BASE_URL
        api_key = os.environ.get("OPENAI_API_KEY", "")
        logger.info(f"OpenAI 客户端配置 - model: {model}, base_url: {base_url}")
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or os.environ.get("ANTHROPIC_API_BASE") or DEFAULT_ANTHROPIC_BASE_URL
        api_key = _first_env("ANTHROPIC_API_KEY", "RIGHT_CODES_API_KEY", "OPENAI_API_KEY")
        logger.info(f"Anthropic 客户端配置 - model: {model}, base_url: {base_url}")
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )

    # Ollama provider
    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    logger.info(f"Ollama 客户端配置 - model: {model}, host: {host}")
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
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
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
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

    # 收集需要脱敏的环境变量名称
    configured_secret_names = _configured_secret_names(args)
    logger.debug(f"敏感环境变量数量: {len(configured_secret_names)}")

    # 构建工作区上下文，包含文件快照和 git 状态
    workspace = WorkspaceContext.build(args.cwd)
    logger.info(f"工作区路径: {workspace.cwd}, 分支: {workspace.branch}")

    # 初始化 session 存储器
    store = SessionStore(workspace.repo_root + "/.pico/sessions")
    logger.debug(f"Session 存储路径: {workspace.repo_root}/.pico/sessions")

    # 构建模型客户端
    model = _build_model_client(args)

    # 判断是恢复旧 session 还是创建新 session
    session_id = args.resume
    dry_run = bool(getattr(args, "dry_run", False))

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
            dry_run=dry_run,
            secret_env_names=configured_secret_names,
        )

    logger.info("创建新的 Pico 实例")
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        dry_run=dry_run,
        secret_env_names=configured_secret_names,
    )


def build_arg_parser():
    """构建命令行参数解析器。

    定义所有支持的命令行参数，包括：
    - prompt: one-shot 模式的提示词
    - --cwd: 工作区目录
    - --provider: 模型后端选择
    - --model: 模型名称
    - --host: Ollama 服务器地址
    - --base-url: API 基础 URL
    - --approval: 审批策略
    - --dry-run: 模拟模式
    等
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for Ollama, OpenAI-compatible, or Anthropic-compatible models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic"), default="openai", help="Model backend to use.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, OPENAI_MODEL for openai, and ANTHROPIC_MODEL for anthropic when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate risky tools without running shell commands or writing files.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    return parser


def main(argv=None):
    """命令行主入口函数。

    解析参数 -> 构建 agent -> 显示欢迎信息 -> 进入 one-shot 或交互模式。
    """
    logger.info("pico 启动中...")
    args = build_arg_parser().parse_args(argv)
    logger.debug(f"命令行参数: {args}")

    agent = build_agent(args)
    logger.info("Pico agent 构建完成")

    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            logger.info(f"one-shot 模式，提示词长度: {len(prompt)} 字符")
            print()
            try:
                result = agent.ask(prompt)
                print(result)
                logger.info("one-shot 执行完成")
            except RuntimeError as exc:
                logger.error(f"执行出错: {exc}")
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    logger.info("进入交互模式")
    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
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

        print()
        try:
            logger.debug(f"处理用户输入: {user_input[:50]}...")
            result = agent.ask(user_input)
            print(result)
        except RuntimeError as exc:
            logger.error(f"执行出错: {exc}")
            print(str(exc), file=sys.stderr)
