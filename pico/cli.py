"""命令行入口。

这个模块负责把“用户怎么启动 pico”翻译成 runtime 能理解的对象：
解析参数、构建窄模型适配器、工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import math
import shutil
import sys
import textwrap

from .config import PicoConfig
from .env import load_project_env, provider_env
from .execution import ExecutionContext
from .providers.clients import DEFAULT_OPENAI_BASE_URL, OpenAICompatibleModelClient
from .runtime import Pico
from .session_store import SessionStore
from .trace import TracePrinter
from .workspace import Workspace, middle

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
    /state   Show the current Run state.
    /session Show the path to the saved session file.
    /reset   Stop the active Run and clear the Session pointer.
    /exit    Exit the agent.
    """
).strip()


DEFAULT_OPENAI_MODEL = "gpt-5.4"


def _terminal_approval(name, args, plan):
    request = {"arguments": args, "targets": [str(target) for _path, target in plan.paths],
               "operation": plan.operation}
    try:
        answer = input(
            f"approve {name} {json.dumps(request, ensure_ascii=True)}? [y/N] "
        )
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _build_model_client(args):
    model = args.model or provider_env("PICO_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    configured_base_url = provider_env("PICO_OPENAI_API_BASE")
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key and not configured_base_url:
        raise RuntimeError(
            "Set PICO_OPENAI_API_KEY or PICO_OPENAI_API_BASE for an intentional no-auth endpoint."
        )
    try:
        temperature = float(provider_env("PICO_OPENAI_TEMPERATURE", "0.2"))
        timeout = float(provider_env("PICO_OPENAI_TIMEOUT", "300"))
    except ValueError:
        raise ValueError(
            "PICO_OPENAI_TEMPERATURE and PICO_OPENAI_TIMEOUT must be numbers"
        ) from None
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("PICO_OPENAI_TEMPERATURE must be between 0 and 2")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PICO_OPENAI_TIMEOUT must be positive")
    return OpenAICompatibleModelClient(
        model=model,
        base_url=configured_base_url or DEFAULT_OPENAI_BASE_URL,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
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

    observation = agent.workspace.observe(
        command_runner=agent.dependencies.command_runner,
        execution_context=ExecutionContext.root(max_seconds=5),
    )
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
                + middle(agent.workspace.cwd, inner - 11)
            ),
            pair("MODEL", model, "HEAD", observation.head),
            pair(
                "MODE",
                agent.config.mode,
                "SESSION",
                agent.session.id,
            ),
            row(
                "VERIFY     "
                + (agent.config.verification_command or "unavailable")
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
    workspace = Workspace.build(args.cwd)
    load_project_env(workspace.root, boundary=workspace.root)
    store = SessionStore(workspace.root / ".pico" / "sessions")
    config = PicoConfig(mode=args.mode)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest_active()
        if not session_id:
            raise ValueError("no unfinished Session is available to resume")
    session = store.load(session_id) if session_id else store.create(workspace.root)
    return Pico(
        model_client=_build_model_client(args),
        workspace=workspace,
        session=session,
        trace=TracePrinter(sys.stderr) if args.trace else None,
        config=config,
        approval_handler=_terminal_approval,
    )


def _run_state_text(agent):
    task = agent.run.projection
    if task.contract is None:
        return "Run: not started"
    changed = ", ".join(task.evidence.changed_paths) or "none"
    return "\n".join(
        (
            f"Goal: {task.contract.goal}",
            f"Status: {task.status}",
            f"Changed: {changed}",
        )
    )


def _outcome_summary(agent, outcome):
    changed = ", ".join(outcome.changed_paths) or "none"
    if not outcome.changed_paths:
        verification = "not required"
    elif not agent.config.verification_command:
        verification = "unavailable"
    else:
        records = agent.run.evidence.verifications
        verification = (
            str(records[-1].get("status", "not run"))
            if records
            else "not run"
        )
    lines = [
        f"Status: {outcome.status}",
        f"Changed: {changed}",
        f"Verification: {verification}",
        f"Run: {outcome.run_id}",
    ]
    if outcome.status != "completed" and outcome.stop_reason:
        lines.insert(1, f"Stop reason: {outcome.stop_reason}")
    if outcome.final_diff and outcome.final_diff.external_paths:
        lines.append("Diff includes observed external changes (not solely Agent-authored): "
                     + ", ".join(outcome.final_diff.external_paths))
    return "\n".join(lines)


def _print_outcome(agent, outcome):
    print(_outcome_summary(agent, outcome))
    print()
    print(outcome.answer)


def build_arg_parser():
    defaults = PicoConfig()
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="A readable local coding-agent runtime.",
        allow_abbrev=False,
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--trace", action="store_true",
        help="Print live Runtime events to stderr (without prompt or file contents).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to PICO_OPENAI_MODEL or gpt-5.4.",
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
            "automates bounded file changes but never exposes run_shell."
        ),
    )
    return parser


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
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
            _print_outcome(agent, outcome)
            return 0 if outcome.status == "completed" else 1
        return 0

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 Run Log 和由它投影的运行状态会跨恢复轮次延续。
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
            print(_run_state_text(agent))
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
            outcome = agent.ask(user_input)
            _print_outcome(agent, outcome)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
