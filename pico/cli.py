"""命令行入口。

这个模块负责把“用户怎么启动 pico”翻译成 runtime 能理解的对象：
解析参数、构建窄模型适配器、工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import os
import shutil
import sys
import textwrap

from .config import load_project_env, provider_env
from .providers.clients import DEFAULT_OPENAI_BASE_URL, OpenAICompatibleModelClient
from .runtime import Pico, PicoConfig, SessionStore
from .working_state import WorkingState
from .workspace import WorkspaceContext, middle

DEFAULT_SECRET_ENV_NAMES = (
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PICO_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "pico"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /state   Show the current Run WorkingState.
    /session Show the path to the saved session file.
    /reset   Stop the active Run and clear the Session pointer.
    /exit    Exit the agent.
    """
).strip()


DEFAULT_OPENAI_MODEL = "gpt-5.4"
SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def _effective_model(args):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. OpenAI-compatible 环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    return provider_env("PICO_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper() for item in extra_names.split(",") if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    """Build the single supported Responses transport."""
    model = _effective_model(args)
    configured_base_url = getattr(args, "base_url", None) or provider_env(
        "PICO_OPENAI_API_BASE"
    )
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key and not configured_base_url:
        raise RuntimeError(
            "PICO_OPENAI_API_KEY is not configured. Set it in the project "
            ".env.local/.env, or pass --base-url for an intentional no-auth "
            "OpenAI-compatible endpoint."
        )
    base_url = configured_base_url or DEFAULT_OPENAI_BASE_URL
    return OpenAICompatibleModelClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=args.temperature,
        timeout=args.openai_timeout,
    )


def build_welcome(agent, model):
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
            row(
                "WORKSPACE  "
                + middle(agent.workspace.context.cwd, inner - 11)
            ),
            pair("MODEL", model, "BRANCH", agent.workspace.context.branch),
            pair(
                "MODE",
                agent.config.mode,
                "SESSION",
                agent.session.data["id"],
            ),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Pico 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    Responses client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。
    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pico`，或一个从旧 session 恢复出来的 `Pico`
    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型和 session。
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root, boundary=workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace.repo_root + "/.pico/sessions")
    model = _build_model_client(args)
    config = PicoConfig(
        mode=args.mode,
        max_agent_turns=args.max_agent_turns,
        max_tool_executions=args.max_tool_executions,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=set(configured_secret_names),
        turn_timeout_seconds=args.turn_timeout,
        provider_context_limit_tokens=args.provider_context_limit,
        compaction_reserve_tokens=args.compaction_reserve_tokens,
        compaction_keep_recent_tokens=args.compaction_keep_recent_tokens,
        verification_command=args.verify_command,
    )

    def child_model_client_factory(_spec):
        return _build_model_client(args)

    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return Pico(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session=store.load(session_id),
            config=config,
            subagent_model_client_factory=child_model_client_factory,
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        config=config,
        subagent_model_client_factory=child_model_client_factory,
    )


def _working_state_text(agent):
    task = agent.run.task
    if task is None:
        return WorkingState().render_panel()
    return "Task goal:\n- " + task.contract.goal + "\n\n" + task.working.render_panel()


def build_arg_parser():
    defaults = PicoConfig()
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Minimal coding agent runtime for an OpenAI-compatible "
            "Responses endpoint."
        ),
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to PICO_OPENAI_MODEL or gpt-5.4.",
    )
    parser.add_argument(
        "--base-url", default=None, help="OpenAI-compatible API base URL."
    )
    parser.add_argument(
        "--openai-timeout",
        type=int,
        default=300,
        help="OpenAI-compatible request timeout in seconds.",
    )
    parser.add_argument(
        "--resume", default=None, help="Session id to resume or 'latest'."
    )
    parser.add_argument(
        "--mode",
        choices=("ask", "code", "auto"),
        default=defaults.mode,
        help=(
            "Ask is observation-only; Code asks before risky actions; Auto "
            "automates bounded file changes but never exposes run_command."
        ),
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help=(
            "Extra environment variable names to treat as secrets for "
            "event/artifact redaction."
        ),
    )
    parser.add_argument(
        "--max-agent-turns",
        type=int,
        default=defaults.max_agent_turns,
        help="Maximum main Agent model turns in one active ask/resume call.",
    )
    parser.add_argument(
        "--max-tool-executions",
        type=int,
        default=defaults.max_tool_executions,
        help="Optional maximum executed tool calls per request; unset means no tool limit.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=defaults.max_new_tokens,
        help=(
            "Maximum total model output tokens per action, including reasoning "
            "tokens, visible text, and function-call arguments."
        ),
    )
    parser.add_argument(
        "--turn-timeout",
        type=int,
        default=defaults.turn_timeout_seconds,
        help="Active ask/resume deadline in seconds.",
    )
    parser.add_argument(
        "--provider-context-limit",
        type=int,
        default=defaults.provider_context_limit_tokens,
        help="Model context window used for prompt budgeting, compaction, and Responses rotation.",
    )
    parser.add_argument(
        "--compaction-reserve-tokens",
        type=int,
        default=defaults.compaction_reserve_tokens,
        help="Context tokens reserved before automatic Run Log compaction.",
    )
    parser.add_argument(
        "--compaction-keep-recent-tokens",
        type=int,
        default=defaults.compaction_keep_recent_tokens,
        help="Approximate recent Run Log tokens retained after compaction.",
    )
    parser.add_argument(
        "--verify-command",
        default=defaults.verification_command,
        help=(
            "Trusted local verification command owned by the Runtime; "
            "empty means unavailable."
        ),
    )
    parser.add_argument(
        "--temperature", type=float, default=0.2, help="Sampling temperature."
    )
    return parser


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == ["run"]:
        from .run_cli import run_main

        return run_main(raw_argv[1:])
    args = build_arg_parser().parse_args(raw_argv)
    try:
        agent = build_agent(args)
    except (RuntimeError, ValueError) as exc:
        print(f"pico: {exc}", file=sys.stderr)
        return 2

    model = getattr(
        agent.model_client, "model", getattr(args, "model", DEFAULT_OPENAI_MODEL)
    )
    print(build_welcome(agent, model=model))

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                outcome = agent.ask(prompt)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(outcome.answer)
            return 0 if outcome.status == "completed" else 1
        return 0

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 Run Log 和由它投影的 WorkingState 会跨恢复轮次延续。
        try:
            user_input = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/state":
            print(_working_state_text(agent))
            continue
        if user_input == "/session":
            print(agent.session.path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            print(agent.ask(user_input).answer)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
