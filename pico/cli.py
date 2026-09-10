"""Command-line composition and presentation."""

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

DEFAULT_OPENAI_MODEL = "gpt-5.4"
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
    /state   Show the current Runtime run state.
    /session Show the path to the saved Session snapshot.
    /reset   Reset the current Session.
    /exit    Exit the agent.
    """
).strip()
def _terminal_approval(name, args, plan):
    request = {
        "arguments": args,
        "targets": [str(target) for _logical, target in plan.paths],
        "operation": plan.operation or name,
    }
    try:
        answer = input(
            f"approve {name} {json.dumps(request, ensure_ascii=True)}? [y/N] "
        )
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _build_model_client(args):
    model = args.model or provider_env("PICO_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    configured_base = provider_env("PICO_OPENAI_API_BASE")
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key and not configured_base:
        raise RuntimeError(
            "Set PICO_OPENAI_API_KEY or PICO_OPENAI_API_BASE for an intentional no-auth endpoint."
        )
    try:
        temperature = float(provider_env("PICO_OPENAI_TEMPERATURE", "0.2"))
        timeout = float(provider_env("PICO_OPENAI_TIMEOUT", "300"))
    except ValueError:
        raise ValueError("PICO_OPENAI_TEMPERATURE and PICO_OPENAI_TIMEOUT must be numbers") from None
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("PICO_OPENAI_TEMPERATURE must be between 0 and 2")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PICO_OPENAI_TIMEOUT must be positive")
    return OpenAICompatibleModelClient(
        model=model,
        base_url=configured_base or DEFAULT_OPENAI_BASE_URL,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
    )


def build_agent(args):
    workspace = Workspace.build(args.cwd)
    load_project_env(workspace.root, boundary=workspace.root)
    config = PicoConfig(mode=args.mode)
    store = SessionStore(workspace.root / ".pico" / "sessions")
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest_active()
        if not session_id:
            raise ValueError("no unfinished Session is available")
    constructor = Pico.resume if session_id else Pico.create
    session_args = (
        {"session": store.load(session_id, workspace.root)}
        if session_id
        else {"session_store": store}
    )
    return constructor(
        _build_model_client(args),
        workspace,
        config=config,
        trace=TracePrinter(sys.stderr) if args.trace else None,
        approval_handler=_terminal_approval,
        **session_args,
    )


def build_arg_parser():
    defaults = PicoConfig()
    parser = argparse.ArgumentParser(
        description="A readable local coding-agent runtime.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--resume", help="Session id or latest")
    parser.add_argument("--mode", choices=("ask", "code", "auto"), default=defaults.mode)
    parser.add_argument("--model")
    parser.add_argument("--trace", action="store_true")
    return parser


def _print_outcome(outcome):
    print(f"Status: {outcome.status}")
    print(f"Verification: {outcome.verification}")
    print(f"Session: {outcome.session_id}")
    if outcome.stop_reason:
        print(f"Stop reason: {outcome.stop_reason}")
    print()
    print(outcome.answer)


def build_welcome(agent, model):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(value):
        body = middle(value, inner)
        return f"| {body.ljust(inner)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(value):
        body = middle(value, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        return middle(f"{label:<9} {value}", size).ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    observation = agent.workspace.observe(
        command_runner=agent.command_runner,
        execution_context=ExecutionContext.root(max_seconds=5),
    )
    rows = [center(value) for value in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "HEAD", observation.head),
            pair("MODE", agent.config.mode, "SESSION", agent.session.id),
            row(""),
        ]
    )
    return "\n".join([divider("="), *rows, divider("=")])


def main(argv=None):
    args = build_arg_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        agent = build_agent(args)
    except (RuntimeError, ValueError) as exc:
        print(f"pico: {exc}", file=sys.stderr)
        return 2
    model = getattr(agent.model_client, "model", args.model or DEFAULT_OPENAI_MODEL)
    print(build_welcome(agent, model))
    if args.prompt:
        outcome = agent.ask(" ".join(args.prompt).strip())
        _print_outcome(outcome)
        return 0 if outcome.status == "completed" else 1
    while True:
        try:
            value = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not value:
            continue
        if value in {"/exit", "/quit"}:
            return 0
        if value == "/help":
            print(HELP_DETAILS)
            continue
        if value == "/session":
            print(agent.session.path)
            continue
        if value == "/state":
            print(json.dumps(agent.session.run, indent=2, ensure_ascii=False))
            continue
        if value == "/reset":
            agent.reset()
            print("session reset")
            continue
        _print_outcome(agent.ask(value))
